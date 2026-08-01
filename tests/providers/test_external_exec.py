from __future__ import annotations

import asyncio
import os
import signal
import tempfile
import textwrap
import unittest
from pathlib import Path

from dynamic_firm.providers.external_exec import ExternalExecProvider, ExternalExecProviderConfig
from dynamic_firm.runtime.models import ModelMessage, ModelRequest, ToolSchema
from dynamic_firm.runtime.ports import CancellationToken, ModelProviderError, OperationCancelled


class ExternalExecProviderTests(unittest.TestCase):
    def _bridge(self, root: Path, response: str) -> Path:
        path = root / "bridge.py"
        path.write_text(
            "#!" + os.sys.executable + "\n"
            + "import json, sys\n"
            + "request = json.load(sys.stdin)\n"
            + "assert request['schema'] == 'noruct.external-model-exec.v1'\n"
            + "print(" + repr(response) + ")\n",
            encoding="utf-8",
        )
        path.chmod(0o700)
        return path

    def _hanging_bridge_with_term_ignoring_child(self, root: Path, pid_path: Path) -> Path:
        path = root / "hanging-bridge.py"
        path.write_text(
            "#!" + os.sys.executable + "\n"
            + "import json, signal, subprocess, sys, time\n"
            + "from pathlib import Path\n"
            + "json.load(sys.stdin)\n"
            + "child = subprocess.Popen([sys.executable, '-c', "
            + repr("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)")
            + "])\n"
            + "Path(" + repr(str(pid_path)) + ").write_text(str(child.pid), encoding='utf-8')\n"
            + "time.sleep(30)\n",
            encoding="utf-8",
        )
        path.chmod(0o700)
        return path

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
        raise AssertionError(f"fixture child process survived cancellation: {pid}")

    @staticmethod
    def _cleanup_fixture_pid(pid: int | None) -> None:
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _request(self, *, tools=()) -> ModelRequest:
        return ModelRequest(
            messages=(ModelMessage(role="user", content="hello"),),
            tools=tools,
            model_profile="subscription",
            run_id="run-test",
            call_index=1,
        )

    def test_completion_uses_private_json_protocol_and_never_needs_api_key(self) -> None:
        response = '{"kind":"completion","completion":{"summary":"done","artifact_refs":[],"acceptance_evidence":[],"unresolved_issues":[],"suggested_followups":[],"observations":[],"signals":[]}}'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = self._bridge(root, response)
            provider = ExternalExecProvider(ExternalExecProviderConfig(workspace=root, command=str(bridge), model="subscription-model"))
            result = asyncio.run(provider.complete(self._request(), CancellationToken()))
        self.assertIsNotNone(result.completion)
        assert result.completion is not None
        self.assertEqual(result.completion.summary, "done")

    def test_tool_calls_stay_inside_parent_declared_tool_contract(self) -> None:
        response = '{"kind":"tool_call","tool_call":{"id":"call-1","function":{"name":"read_workspace_file","arguments":"{\\"workspace_id\\":\\"noruct-workspace\\",\\"path\\":\\"README.md\\"}"}}}'
        schema = ToolSchema(name="read_workspace_file", description="read", input_schema={"type": "object"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = self._bridge(root, response)
            provider = ExternalExecProvider(ExternalExecProviderConfig(workspace=root, command=str(bridge), model="subscription-model"))
            result = asyncio.run(provider.complete(self._request(tools=(schema,)), CancellationToken()))
        self.assertEqual(result.tool_calls[0].name, "read_workspace_file")

    def test_invalid_or_non_allowlisted_response_fails_closed(self) -> None:
        response = '{"kind":"tool_call","tool_call":{"id":"call-1","function":{"name":"shell","arguments":"{}"}}}'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = self._bridge(root, response)
            provider = ExternalExecProvider(ExternalExecProviderConfig(workspace=root, command=str(bridge), model="subscription-model"))
            with self.assertRaises(ModelProviderError):
                asyncio.run(provider.complete(self._request(), CancellationToken()))

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_cancellation_reaps_term_ignoring_process_group_child(self) -> None:
        async def exercise() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pid_path = root / "child.pid"
                bridge = self._hanging_bridge_with_term_ignoring_child(root, pid_path)
                provider = ExternalExecProvider(
                    ExternalExecProviderConfig(workspace=root, command=str(bridge), model="subscription-model", timeout_seconds=10)
                )
                cancellation = CancellationToken()
                task = asyncio.create_task(provider.complete(self._request(), cancellation))
                pid: int | None = None
                try:
                    pid = await self._wait_for_fixture_pid(pid_path)
                    cancellation.cancel("test cancellation")
                    with self.assertRaises(OperationCancelled):
                        await asyncio.wait_for(task, 2)
                    await self._assert_pid_gone(pid)
                finally:
                    if not task.done():
                        cancellation.cancel("test cleanup")
                        await asyncio.gather(task, return_exceptions=True)
                    self._cleanup_fixture_pid(pid)

        asyncio.run(exercise())
