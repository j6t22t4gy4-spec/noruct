from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import stat
import sys
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path

from dynamic_firm.coding import (
    CodingWorkRequest,
    CodingWorkerError,
    ShadowWorkspaceService,
    ValidationAttempt,
)
from dynamic_firm.coding.service import _bound_coding_objective
from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.providers.codex_exec import (
    CodexExecCodingWorker,
    CodexExecProvider,
    CodexExecProviderConfig,
    _parse_employee_response,
    _response_schema,
)
from dynamic_firm.providers.openai_compat import _completion_response_format
from dynamic_firm.runtime.models import (
    ModelMessage,
    ModelRequest,
    StructuredOutputRequest,
    ToolSchema,
)
from dynamic_firm.runtime.ports import (
    CancellationToken,
    ModelProviderError,
    OperationCancelled,
)


PROJECT_ROOT = Path(__file__).parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "tiny_repo"


def _write_fake_codex(root: Path) -> Path:
    executable = root / "codex-fixture"
    source = f"#!{sys.executable}\n" + textwrap.dedent(
        r'''
        import json
        import os
        import signal
        import subprocess
        import sys
        import time
        from pathlib import Path

        args = sys.argv[1:]
        if args == ["login", "status"]:
            print("Logged in using ChatGPT")
            raise SystemExit(0)
        if not args or args[0] != "exec":
            raise SystemExit(71)

        required_pairs = {
            "--sandbox": "read-only",
            "--color": "never",
        }
        required_flags = {
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--skip-git-repo-check",
        }
        if not required_flags.issubset(args):
            raise SystemExit(72)
        for flag, value in required_pairs.items():
            if flag not in args or args[args.index(flag) + 1] != value:
                raise SystemExit(73)
        if "--dangerously-bypass-approvals-and-sandbox" in args or "workspace-write" in args:
            raise SystemExit(74)
        config_values = [args[index + 1] for index, item in enumerate(args) if item == "-c"]
        if 'web_search="disabled"' not in config_values:
            raise SystemExit(75)
        if 'shell_environment_policy.inherit="none"' not in config_values:
            raise SystemExit(76)
        disabled = [args[index + 1] for index, item in enumerate(args) if item == "--disable"]
        if set(disabled) != {"multi_agent", "apps", "plugins", "browser_use", "computer_use"}:
            raise SystemExit(81)
        if args[-1] != "-":
            raise SystemExit(77)
        if os.environ.get("NORUCT_API_KEY") or os.environ.get("FAKE_PRIVATE_SECRET"):
            raise SystemExit(78)

        prompt = sys.stdin.read()
        if not prompt or "noruct-openai-codex-read-only-v1" not in prompt:
            raise SystemExit(79)
        if prompt in args:
            raise SystemExit(80)

        model = args[args.index("--model") + 1] if "--model" in args else ""
        if model == "hang":
            time.sleep(30)
        if model == "started-hang":
            print(json.dumps({"type": "thread.started", "thread_id": "thread-contract"}), flush=True)
            time.sleep(30)
        if model == "progressing":
            for _ in range(8):
                print(json.dumps({"type": "item.updated", "status": "working"}), flush=True)
                time.sleep(0.1)
        if model == "large-events":
            print(json.dumps({"type": "item.completed", "data": "x" * 10000}), flush=True)
        if model.startswith("spawn-child-hang:"):
            child = subprocess.Popen([
                sys.executable,
                "-c",
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
            ])
            Path(model.split(":", 1)[1]).write_text(str(child.pid), encoding="utf-8")
            time.sleep(30)

        schema_path = Path(args[args.index("--output-schema") + 1])
        result_path = Path(args[args.index("--output-last-message") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        properties = schema.get("properties", {})
        if "mode" in properties:
            result = {
                "mode": "SOLO",
                "rationale": "One employee is sufficient for this fixture.",
                "assumptions": [],
                "tasks": [{
                    "task_id": "analyze_repository",
                    "objective": "Inspect the repository and report evidence.",
                    "depends_on": [],
                    "required_capabilities": ["repository_analysis"],
                    "acceptance_criteria": ["Return repository evidence."],
                    "risk_level": "LOW"
                }],
                "final_task_id": "analyze_repository"
            }
        elif "kind" in properties:
            selected_tool = next(
                item for item in properties["tool_name"]["enum"] if item
            )
            selected_arguments = (
                '{"workspace_id":"repo","path":"calculator.py"}'
                if selected_tool == "read_workspace_file"
                else "{}"
            )
            if '"role":"tool"' in prompt:
                result = {
                    "kind": "completion",
                    "summary": "Codex subscription backend completed the parent-owned read-only task.",
                    "artifact_refs": [],
                    "acceptance_evidence": ["calculator.py:1"],
                    "unresolved_issues": [],
                    "suggested_followups": [],
                    "observations": ["parent-owned-tool-contract"],
                    "signals": [],
                    "tool_call_id": "",
                    "tool_name": "",
                    "tool_arguments_json": ""
                }
            else:
                result = {
                    "kind": "tool_call",
                    "summary": "",
                    "artifact_refs": [],
                    "acceptance_evidence": [],
                    "unresolved_issues": [],
                    "suggested_followups": [],
                    "observations": [],
                    "signals": [],
                    "tool_call_id": "call-contract-read",
                    "tool_name": selected_tool,
                    "tool_arguments_json": selected_arguments
                }
        elif "summary" in properties:
            result = {
                "summary": "Codex subscription backend completed the read-only task.",
                "artifact_refs": [],
                "acceptance_evidence": ["calculator.py:1"],
                "unresolved_issues": [],
                "suggested_followups": [],
                "observations": ["official-codex-exec-contract"],
                "signals": []
            }
        else:
            result = {"answer": "structured-result"}

        result_path.write_text(json.dumps(result), encoding="utf-8")
        print(json.dumps({"type": "thread.started", "thread_id": "thread-contract"}), flush=True)
        print(json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 13, "cached_input_tokens": 2, "output_tokens": 7}
        }), flush=True)
        ''')
    executable.write_text(source, encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def _write_fake_coding_codex(root: Path) -> Path:
    executable = root / "codex-coding-fixture"
    source = f"#!{sys.executable}\n" + textwrap.dedent(
        r'''
        import json
        import os
        import sys
        from pathlib import Path

        args = sys.argv[1:]
        if not args or args[0] != "exec":
            raise SystemExit(61)
        if "--sandbox" not in args or args[args.index("--sandbox") + 1] != "workspace-write":
            raise SystemExit(62)
        required = {
            "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--strict-config", "--skip-git-repo-check"
        }
        if not required.issubset(args):
            raise SystemExit(63)
        disabled = [args[index + 1] for index, item in enumerate(args) if item == "--disable"]
        if set(disabled) != {"multi_agent", "apps", "plugins", "browser_use", "computer_use"}:
            raise SystemExit(64)
        configs = [args[index + 1] for index, item in enumerate(args) if item == "-c"]
        if 'web_search="disabled"' not in configs:
            raise SystemExit(65)
        if 'shell_environment_policy.inherit="none"' not in configs:
            raise SystemExit(66)
        if 'approval_policy="never"' not in configs:
            raise SystemExit(67)
        if "--dangerously-bypass-approvals-and-sandbox" in args:
            raise SystemExit(68)
        if os.environ.get("NORUCT_API_KEY") or os.environ.get("FAKE_PRIVATE_SECRET"):
            raise SystemExit(69)

        workspace = Path(args[args.index("-C") + 1]).resolve()
        if Path.cwd().resolve() != workspace or not workspace.parent.name.startswith("noruct-shadow-"):
            raise SystemExit(70)
        prompt = sys.stdin.read()
        if "noruct-codex-shadow-coding-v2" not in prompt or prompt in args:
            raise SystemExit(71)
        (workspace / "app.py").write_text("after\n", encoding="utf-8")
        result_path = Path(args[args.index("--output-last-message") + 1])
        result_path.write_text(json.dumps({
            "summary": "Changed the disposable shadow copy.",
            "acceptance_evidence": ["app.py changed"],
            "unresolved_issues": [],
            "observations": ["shadow-only"],
            "suggested_followups": [],
            "verification_commands": ["printf verified"]
        }), encoding="utf-8")
        print(json.dumps({"type": "thread.started", "thread_id": "thread-shadow"}), flush=True)
        print(json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 21, "cached_input_tokens": 3, "output_tokens": 8}
        }), flush=True)
        ''')
    executable.write_text(source, encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def _model_request(*, model_profile: str = "codex-default", tools=()) -> ModelRequest:
    return ModelRequest(
        messages=(ModelMessage("user", "Inspect the repository"),),
        tools=tuple(tools),
        model_profile=model_profile,
        run_id="run-codex-contract",
        call_index=1,
    )


class CodexExecProviderTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _wait_for_fixture_pid(path: Path) -> int:
        for _ in range(50):
            if path.is_file():
                return int(path.read_text(encoding="utf-8"))
            await asyncio.sleep(0.02)
        raise AssertionError("fixture child PID was not recorded")

    @staticmethod
    async def _assert_pid_gone(pid: int) -> None:
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"fixture child process survived deadline: {pid}")

    @staticmethod
    def _cleanup_fixture_pid(pid: int | None) -> None:
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_default_selector_is_not_forwarded_as_a_codex_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = _write_fake_codex(root)
            provider = CodexExecProvider(
                CodexExecProviderConfig(
                    workspace=FIXTURE_ROOT,
                    command=str(command),
                    model="codex-default",
                )
            )
            rendered = provider._exec_command(
                schema_path=root / "schema.json",
                result_path=root / "result.json",
            )

        self.assertNotIn("--model", rendered)

    def test_explicit_selector_is_forwarded_as_a_codex_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = _write_fake_codex(root)
            provider = CodexExecProvider(
                CodexExecProviderConfig(
                    workspace=FIXTURE_ROOT,
                    command=str(command),
                    model="gpt-5.6",
                )
            )
            rendered = provider._exec_command(
                schema_path=root / "schema.json",
                result_path=root / "result.json",
            )

        self.assertEqual(rendered[rendered.index("--model") + 1], "gpt-5.6")

    def test_completion_schema_keeps_required_output_in_summary(self) -> None:
        schema = _completion_response_format()["json_schema"]["schema"]

        self.assertIn(
            "final deliverable text",
            schema["properties"]["summary"]["description"],
        )
        self.assertIn(
            "does not replace required content in summary",
            schema["properties"]["acceptance_evidence"]["description"],
        )

    async def test_tool_free_request_explicitly_disables_workspace_inspection(self) -> None:
        prompt = CodexExecProvider._prompt(
            (ModelMessage("user", "hello"),),
            "dynamic_firm_employee_completion",
            tools=(),
        )
        self.assertIn("Do not inspect the workspace or run shell commands", prompt)

    async def test_parent_tool_request_forbids_child_workspace_inspection(self) -> None:
        prompt = CodexExecProvider._prompt(
            (ModelMessage("user", "read one fixture"),),
            "dynamic_firm_employee_completion",
            tools=(ToolSchema("read_workspace_file", "Read", {"type": "object"}),),
        )

        self.assertIn("parent-tool contract", prompt)
        self.assertIn("MUST be exactly one", prompt)
        self.assertIn("not included in these messages", prompt)
        self.assertIn("Prefer read_workspace_file", prompt)
        self.assertIn("never repeat", prompt)
        self.assertIn("Do not inspect the workspace or run shell commands yourself", prompt)
        self.assertNotIn("Use the Codex read-only shell", prompt)

    async def test_parent_tool_schema_keeps_cli_compatible_tool_and_completion_values(self) -> None:
        schema = _response_schema(
            (
                ToolSchema("read_workspace_file", "Read", {"type": "object"}),
                ToolSchema("list_workspace_files", "List", {"type": "object"}),
            )
        )

        self.assertEqual(
            schema["properties"]["tool_name"]["enum"],
            ["", "read_workspace_file", "list_workspace_files"],
        )
        self.assertIn(
            "kind=tool_call",
            schema["properties"]["tool_name"]["description"],
        )
        self.assertNotIn("allOf", schema)

    async def test_completion_with_parent_tool_fields_is_rejected(self) -> None:
        with self.assertRaisesRegex(ModelProviderError, "mixed parent tool fields"):
            _parse_employee_response(
                {
                    "kind": "completion",
                    "summary": "unsupported completion",
                    "artifact_refs": [],
                    "acceptance_evidence": [],
                    "unresolved_issues": [],
                    "suggested_followups": [],
                    "observations": [],
                    "signals": [],
                    "tool_call_id": "",
                    "tool_name": "read_workspace_file",
                    "tool_arguments_json": "",
                },
                (ToolSchema("read_workspace_file", "Read", {"type": "object"}),),
            )

    async def test_read_only_exec_uses_schema_stdin_usage_and_sanitized_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = _write_fake_codex(root)
            provider = CodexExecProvider(
                CodexExecProviderConfig(
                    workspace=FIXTURE_ROOT,
                    command=str(command),
                    model="contract-model",
                    timeout_seconds=5,
                ),
                environ={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": str(root),
                    "NORUCT_API_KEY": "must-not-reach-codex",
                    "FAKE_PRIVATE_SECRET": "must-not-reach-codex",
                },
            )
            result = await provider.complete(
                _model_request(
                    tools=(
                        ToolSchema("read_workspace_file", "Read", {"type": "object"}),
                        ToolSchema("list_workspace_files", "List", {"type": "object"}),
                    )
                ),
                CancellationToken(),
            )

        self.assertIsNone(result.completion)
        self.assertEqual(result.tool_calls[0].name, "read_workspace_file")
        self.assertEqual(
            result.tool_calls[0].arguments,
            {"workspace_id": "repo", "path": "calculator.py"},
        )
        self.assertEqual(result.provider_request_id, "thread-contract")
        self.assertEqual(result.usage.input_tokens, 13)
        self.assertEqual(result.usage.cached_input_tokens, 2)
        self.assertEqual(result.usage.output_tokens, 7)

    async def test_structured_compiler_contract_is_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            command = _write_fake_codex(Path(temporary))
            provider = CodexExecProvider(
                CodexExecProviderConfig(workspace=FIXTURE_ROOT, command=str(command), timeout_seconds=5)
            )
            result = await provider.complete_structured(
                StructuredOutputRequest(
                    messages=(ModelMessage("user", "Return an answer"),),
                    schema_name="answer_contract",
                    json_schema={
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                    model_profile="codex-default",
                    request_id="request-structured",
                ),
                CancellationToken(),
            )

        self.assertEqual(result.value, {"answer": "structured-result"})

    async def test_parent_owned_write_intent_is_allowed_without_child_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            command = _write_fake_codex(Path(temporary))
            provider = CodexExecProvider(
                CodexExecProviderConfig(workspace=FIXTURE_ROOT, command=str(command))
            )
            result = await provider.complete(
                _model_request(
                    tools=(ToolSchema("write_workspace_file", "Write", {"type": "object"}),)
                ),
                CancellationToken(),
            )

        self.assertEqual(result.tool_calls[0].name, "write_workspace_file")

    async def test_cancellation_terminates_the_codex_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            command = _write_fake_codex(Path(temporary))
            provider = CodexExecProvider(
                CodexExecProviderConfig(
                    workspace=FIXTURE_ROOT,
                    command=str(command),
                    model="hang",
                    timeout_seconds=10,
                )
            )
            cancellation = CancellationToken()
            task = asyncio.create_task(provider.complete(_model_request(), cancellation))
            await asyncio.sleep(0.1)
            cancellation.cancel("contract cancellation")
            with self.assertRaises(OperationCancelled):
                await asyncio.wait_for(task, 2)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    async def test_hard_deadline_reaps_term_ignoring_process_group_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_path = root / "child.pid"
            command = _write_fake_codex(root)
            provider = CodexExecProvider(
                CodexExecProviderConfig(
                    workspace=FIXTURE_ROOT,
                    command=str(command),
                    model=f"spawn-child-hang:{pid_path}",
                    timeout_seconds=1.0,
                    stale_timeout_seconds=10,
                )
            )
            task = asyncio.create_task(provider.complete(_model_request(), CancellationToken()))
            pid: int | None = None
            try:
                pid = await self._wait_for_fixture_pid(pid_path)
                with self.assertRaises(ModelProviderError) as raised:
                    await asyncio.wait_for(task, 2)
                self.assertEqual(raised.exception.code, "MODEL_TIMEOUT")
                await self._assert_pid_gone(pid)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self._cleanup_fixture_pid(pid)

    async def test_cancellation_retains_only_early_request_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            command = _write_fake_codex(Path(temporary))
            provider = CodexExecProvider(
                CodexExecProviderConfig(
                    workspace=FIXTURE_ROOT,
                    command=str(command),
                    model="started-hang",
                    timeout_seconds=10,
                )
            )
            cancellation = CancellationToken()
            task = asyncio.create_task(provider.complete(_model_request(), cancellation))
            for _ in range(50):
                if provider.observed_request_id("run-codex-contract") is not None:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(
                provider.observed_request_id("run-codex-contract"),
                "thread-contract",
            )
            cancellation.cancel("contract cancellation after request identity")
            with self.assertRaises(OperationCancelled):
                await asyncio.wait_for(task, 2)

        self.assertEqual(
            provider.consume_cancelled_request_id("run-codex-contract"),
            "thread-contract",
        )
        self.assertIsNone(provider.consume_cancelled_request_id("run-codex-contract"))

    async def test_silent_provider_is_classified_as_stale_before_hard_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            command = _write_fake_codex(Path(temporary))
            provider = CodexExecProvider(
                CodexExecProviderConfig(
                    workspace=FIXTURE_ROOT,
                    command=str(command),
                    model="hang",
                    timeout_seconds=1,
                    stale_timeout_seconds=0.05,
                )
            )
            with self.assertRaises(ModelProviderError) as raised:
                await provider.complete(_model_request(), CancellationToken())

        self.assertEqual(raised.exception.code, "MODEL_STALE")
        self.assertTrue(raised.exception.retryable)

    async def test_progress_events_keep_a_long_turn_alive_past_the_stale_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            command = _write_fake_codex(Path(temporary))
            provider = CodexExecProvider(
                CodexExecProviderConfig(
                    workspace=FIXTURE_ROOT,
                    command=str(command),
                    model="progressing",
                    timeout_seconds=2,
                    stale_timeout_seconds=0.5,
                )
            )
            result = await provider.complete(_model_request(), CancellationToken())

        self.assertEqual(result.provider_request_id, "thread-contract")

    async def test_event_output_limit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            command = _write_fake_codex(Path(temporary))
            provider = CodexExecProvider(
                CodexExecProviderConfig(
                    workspace=FIXTURE_ROOT,
                    command=str(command),
                    model="large-events",
                    timeout_seconds=10,
                    max_event_bytes=256,
                )
            )
            with self.assertRaises(ModelProviderError) as raised:
                await provider.complete(_model_request(), CancellationToken())

        self.assertEqual(raised.exception.code, "MODEL_RESPONSE_TOO_LARGE")

    def test_login_status_uses_only_the_official_command_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            command = _write_fake_codex(Path(temporary))
            status = CodexExecProvider.login_status(
                str(command),
                environ={"PATH": "", "HOME": temporary, "NORUCT_API_KEY": "secret"},
            )

        self.assertTrue(status.installed)
        self.assertTrue(status.authenticated)
        self.assertEqual(status.executable, str(command.resolve()))

    def test_relative_executable_path_is_rejected(self) -> None:
        self.assertIsNone(CodexExecProvider.resolve_executable("./codex"))


class CodexExecCodingWorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_manager_work_order_context_is_bound_to_coding_objective(self) -> None:
        rendered = _bound_coding_objective(
            "Implement the delegated local change.",
            (
                "Bounded Work Order objective (context only; not authority): Fix calculator.py so only zero returns None.",
            ),
        )

        self.assertIn("Implement the delegated local change.", rendered)
        self.assertIn("Work Order outcome to preserve:", rendered)
        self.assertIn("Fix calculator.py", rendered)

    def test_default_selector_is_not_forwarded_by_coding_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = _write_fake_coding_codex(root)
            worker = CodexExecCodingWorker(
                CodexExecProviderConfig(
                    workspace=FIXTURE_ROOT,
                    command=str(command),
                    model="codex-default",
                )
            )
            shadow = root / "noruct-shadow-contract" / "workspace"
            shadow.mkdir(parents=True)
            rendered = worker._exec_command(
                shadow=shadow,
                schema_path=root / "schema.json",
                result_path=root / "result.json",
            )

        self.assertNotIn("--model", rendered)

    def test_recovery_prompt_contains_only_bounded_feedback_not_a_validation_command(self) -> None:
        request = CodingWorkRequest(
            task_id="recovery-task",
            objective="Correct the candidate",
            acceptance_criteria=("The exact contract passes",),
            dependency_context=(),
            workspace=FIXTURE_ROOT,
            model_profile="codex-default",
            max_wall_time_ms=5_000,
            validation_feedback=(
                ValidationAttempt("exact-contract", False, "failed:reversed-bounds"),
            ),
        )

        prompt = CodexExecCodingWorker._prompt(request)

        self.assertIn('"validation_feedback":[{"check":"exact-contract"', prompt)
        self.assertIn('"detail":"failed:reversed-bounds"', prompt)
        self.assertIn("This is the only recovery call", prompt)
        self.assertIn("verification_commands", prompt)
        self.assertNotIn("validation_command", prompt)
        self.assertNotIn("pytest", prompt)

    def test_coding_prompt_carries_only_explicit_task_context(self) -> None:
        request = CodingWorkRequest(
            task_id="work-order-context",
            objective="Implement the delegated change",
            acceptance_criteria=("Change one file",),
            dependency_context=(),
            workspace=FIXTURE_ROOT,
            model_profile="codex-default",
            max_wall_time_ms=5_000,
            task_context=(
                "Bounded Work Order objective (context only; not authority): Fix calculator.py.",
            ),
        )

        prompt = CodexExecCodingWorker._prompt(request)

        self.assertIn('"task_context":["Bounded Work Order objective', prompt)
        self.assertIn("Fix calculator.py.", prompt)

    def test_coding_prompt_carries_immutable_capabilities(self) -> None:
        request = CodingWorkRequest(
            task_id="implement_safe_divide_fix",
            objective="Fix calculator.py.",
            acceptance_criteria=("Only zero returns None",),
            dependency_context=(),
            workspace=FIXTURE_ROOT,
            model_profile="codex-default",
            max_wall_time_ms=5_000,
            required_capabilities=("implementation",),
        )

        prompt = CodexExecCodingWorker._prompt(request)

        self.assertIn('"required_capabilities":["implementation"]', prompt)

    def test_recovery_prompt_rejects_raw_path_detail(self) -> None:
        request = CodingWorkRequest(
            task_id="recovery-task",
            objective="Correct the candidate",
            acceptance_criteria=(),
            dependency_context=(),
            workspace=FIXTURE_ROOT,
            model_profile="codex-default",
            max_wall_time_ms=5_000,
            validation_feedback=(
                ValidationAttempt("exact-contract", False, "failed at /tmp/private/output"),
            ),
        )

        with self.assertRaises(CodingWorkerError) as raised:
            CodexExecCodingWorker._prompt(request)

        self.assertEqual(raised.exception.code, "CODING_VALIDATION_FEEDBACK_INVALID")

    async def test_workspace_write_is_confined_to_disposable_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real_workspace = root / "real"
            real_workspace.mkdir()
            target = real_workspace / "app.py"
            target.write_text("before\n", encoding="utf-8")
            command = _write_fake_coding_codex(root)
            worker = CodexExecCodingWorker(
                CodexExecProviderConfig(
                    workspace=real_workspace,
                    command=str(command),
                    timeout_seconds=10,
                ),
                environ={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": str(root),
                    "NORUCT_API_KEY": "must-not-reach-codex",
                    "FAKE_PRIVATE_SECRET": "must-not-reach-codex",
                },
            )
            request = CodingWorkRequest(
                task_id="coding-task",
                objective="Change app.py",
                acceptance_criteria=("app.py says after",),
                dependency_context=(),
                workspace=real_workspace,
                model_profile="codex-default",
                max_wall_time_ms=5_000,
            )
            outcome = await ShadowWorkspaceService().execute(
                source_root=real_workspace,
                workspace_id="repo",
                request=request,
                worker=worker,
                cancellation=CancellationToken(),
            )

            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
            self.assertEqual(outcome.worker_result.provider_request_id, "thread-shadow")
            self.assertEqual(outcome.worker_result.usage.input_tokens, 21)
            self.assertEqual(outcome.worker_result.verification_commands, ("printf verified",))
            self.assertEqual(outcome.change_set.files[0].new_content, "after\n")

    async def test_direct_real_workspace_binding_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = _write_fake_coding_codex(root)
            worker = CodexExecCodingWorker(
                CodexExecProviderConfig(workspace=root, command=str(command))
            )
            request = CodingWorkRequest(
                task_id="unsafe-task",
                objective="Unsafe direct edit",
                acceptance_criteria=(),
                dependency_context=(),
                workspace=root,
                model_profile="codex-default",
                max_wall_time_ms=5_000,
            )
            with self.assertRaises(CodingWorkerError) as raised:
                await worker.execute(request, CancellationToken())

        self.assertEqual(raised.exception.code, "CODING_WORKSPACE_INVALID")


class CodexExecCliTests(unittest.TestCase):
    def test_setup_and_doctor_require_no_api_secret(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = _write_fake_codex(root)
            config_path = root / "config.toml"
            setup_exit = main(
                [
                    "--config",
                    str(config_path),
                    "setup",
                    "--provider",
                    "openai-codex",
                    "--codex-command",
                    str(command),
                ],
                stdout=output,
                stderr=error,
            )
            settings = tomllib.loads(config_path.read_text(encoding="utf-8"))
            doctor_output = io.StringIO()
            doctor_exit = main(
                ["--config", str(config_path), "doctor", "--json"],
                stdout=doctor_output,
                stderr=error,
            )
            diagnostic = json.loads(doctor_output.getvalue())

        self.assertEqual(setup_exit, EXIT_OK, error.getvalue())
        self.assertEqual(doctor_exit, EXIT_OK, error.getvalue())
        self.assertEqual(settings["provider"]["kind"], "openai_codex")
        self.assertNotIn("base_url", settings["provider"])
        self.assertNotIn("api_key_env", settings["provider"])
        self.assertTrue(diagnostic["run_ready"])
        self.assertEqual(diagnostic["provider"]["authentication"], "codex_chatgpt_login")
        self.assertEqual(
            diagnostic["provider"]["authority"],
            "parent_approved_host_tools_or_disposable_shadow",
        )
        self.assertEqual(
            diagnostic["provider"]["host_direct_operations"],
            "noruct_user_approval_required",
        )
        self.assertEqual(
            diagnostic["provider"]["product_inclusion"],
            "user_managed_external_runtime_not_bundled",
        )
        self.assertEqual(
            diagnostic["provider"]["cost_accounting"],
            "subscription_quota_usd_unavailable",
        )
        self.assertEqual(diagnostic["provider"]["credential_raw_access"], "prohibited")
        self.assertEqual(
            diagnostic["provider"]["subscription_entitlement"],
            "not_determined_by_noruct",
        )
        self.assertEqual(
            diagnostic["provider"]["external_provider_review"],
            "pending_human_release_review",
        )

    def test_company_run_uses_codex_backend_end_to_end(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = _write_fake_codex(root)
            exit_code = main(
                [
                    "run",
                    "Inspect this repository with my ChatGPT subscription",
                    "--provider",
                    "openai-codex",
                    "--codex-command",
                    str(command),
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(root / "runtime.db"),
                    "--max-model-calls",
                    "2",
                    "--permission-mode",
                    "read-only",
                    "--json",
                ],
                stdout=output,
                stderr=error,
            )
            result = json.loads(output.getvalue())

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["planning_mode"], "SOLO")
        self.assertEqual(result["metrics"]["usage"]["model_calls"], 2)
        self.assertEqual(result["compiler_usage"]["model_calls"], 0)
        self.assertEqual(result["planning_reason"], "SOLO_FIRST_ATTEMPT")
        self.assertEqual(
            result["summary"],
            "Codex subscription backend completed the parent-owned read-only task.",
        )


if __name__ == "__main__":
    unittest.main()
