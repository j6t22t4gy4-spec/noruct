from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.product.acp_server import AcpApprovalPort, AcpStdioServer
from dynamic_firm.product.events import ProductEvent, ProductEventType
from dynamic_firm.runtime.models import ApprovalDecision, ApprovalRequest, ToolEffect, ToolRisk, Usage
from dynamic_firm.runtime.ports import CancellationToken


class AcpStdioServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.output = io.StringIO()
        self.turns: list[tuple[str, str]] = []

        async def runner(session, goal, event_sink, approval_port):
            self.turns.append((session.session_id, goal))
            event_sink(
                ProductEvent(
                    ProductEventType.MODEL_STREAMING,
                    "Streaming editor answer.",
                    data={"stream_kind": "text_delta"},
                )
            )
            return SimpleNamespace(
                summary="Streaming editor answer.",
                metrics=SimpleNamespace(usage=Usage(input_tokens=4, output_tokens=3)),
            )

        self.server = AcpStdioServer(
            state_path=self.root / "runtime.db",
            default_workspace=self.root,
            default_model="test-model",
            provider_binding={
                "provider_kind": "ollama",
                "provider_base_url": "http://127.0.0.1:11434/v1",
                "provider_api_key_env": None,
            },
            permission_mode="ask",
            turn_runner=runner,
            stdin=io.StringIO(),
            stdout=self.output,
            stderr=io.StringIO(),
        )

    async def asyncTearDown(self) -> None:
        self.server._store.close()
        self._directory.cleanup()

    def frames(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    async def test_initialize_new_prompt_list_and_model_are_company_backed(self) -> None:
        initialized = await self.server._handle(
            "initialize", {"protocolVersion": 1, "clientInfo": {"name": "test-editor"}}
        )
        self.assertEqual(initialized["protocolVersion"], 1)
        self.assertTrue(initialized["agentCapabilities"]["loadSession"])

        created = await self.server._handle("session/new", {"cwd": str(self.root), "mcpServers": []})
        session_id = str(created["sessionId"])
        await self.server._handle("session/set_model", {"sessionId": session_id, "modelId": "changed-model"})
        await self.server._handle(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": "Inspect this repository."}]},
        )
        await asyncio.sleep(0)
        listed = await self.server._handle("session/list", {"cwd": str(self.root)})

        self.assertEqual(self.turns, [(session_id, "Inspect this repository.")])
        self.assertEqual(listed["sessions"][0]["sessionId"], session_id)
        self.assertEqual(self.server._store.resolve(session_id).model, "changed-model")
        updates = [frame for frame in self.frames() if frame.get("method") == "session/update"]
        self.assertTrue(any(item["params"]["update"]["sessionUpdate"] == "agent_message_chunk" for item in updates))
        self.assertTrue(any(item["params"]["update"]["sessionUpdate"] == "usage_update" for item in updates))

    async def test_invalid_prompt_and_workspace_are_rejected(self) -> None:
        created = await self.server._handle("session/new", {"cwd": str(self.root)})
        session_id = str(created["sessionId"])
        with self.assertRaisesRegex(ValueError, "text prompt blocks"):
            await self.server._handle(
                "session/prompt", {"sessionId": session_id, "prompt": [{"type": "image"}]}
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            await self.server._handle(
                "session/load", {"sessionId": session_id, "cwd": str(self.root.parent)}
            )

    async def test_client_approval_response_maps_to_runtime_decision(self) -> None:
        created = await self.server._handle("session/new", {"cwd": str(self.root)})
        session_id = str(created["sessionId"])
        request = ApprovalRequest(
            action_id="action-1",
            run_id="run-1",
            job_id="job-1",
            task_id="task-1",
            employee_id="employee-1",
            tool_name="write_workspace_file",
            effect=ToolEffect.WRITE,
            risk=ToolRisk.MEDIUM,
            resource_key="workspace:repo:example.txt",
            preview="Write example.txt",
            allow_session=True,
        )
        task = asyncio.create_task(
            AcpApprovalPort(self.server, session_id).request(request, CancellationToken())
        )
        await asyncio.sleep(0)
        frame = self.frames()[-1]
        self.assertEqual(frame["method"], "session/request_permission")
        self.server._accept_client_response(
            {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "allow_session"}},
            }
        )
        self.assertEqual(await task, ApprovalDecision.ALLOW_SESSION)

    async def test_stdio_drains_already_read_requests_before_eof(self) -> None:
        frames = "\n".join(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": 1},
                    }
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "session/new",
                        "params": {"cwd": str(self.root)},
                    }
                ),
                "",
            )
        )
        output = io.StringIO()

        async def runner(_session, _goal, _event_sink, _approval_port):
            raise AssertionError("A protocol setup frame must not start a Company Job")

        server = AcpStdioServer(
            state_path=self.root / "stdio-runtime.db",
            default_workspace=self.root,
            default_model="test-model",
            provider_binding={},
            permission_mode="read-only",
            turn_runner=runner,
            stdin=io.StringIO(frames),
            stdout=output,
            stderr=io.StringIO(),
        )
        self.assertEqual(await server.serve(), 0)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual({item["id"] for item in responses}, {1, 2})
        self.assertIn("sessionId", responses[1]["result"])


class AcpCliTests(unittest.TestCase):
    def test_acp_check_uses_normal_provider_configuration_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "noruct.toml"
            config.write_text(
                '[provider]\nkind = "ollama"\nmodel = "fixture-model"\nno_auth = true\n',
                encoding="utf-8",
            )
            output = io.StringIO()
            self.assertEqual(
                main(
                    ["--config", str(config), "acp", "--workspace", str(root), "--check"],
                    stdout=output,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            self.assertIn("Noruct ACP check OK", output.getvalue())
