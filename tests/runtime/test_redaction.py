from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.models import CompletionEnvelope, ModelResponse, ToolCall
from dynamic_firm.runtime.prompt import PromptBuilder
from dynamic_firm.runtime.redaction import redact_tool_output
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import FixtureReader, ToolRegistry
from tests.runtime.helpers import make_request


class RuntimeRedactionTests(unittest.IsolatedAsyncioTestCase):
    def test_prompt_is_redacted_before_hashing(self) -> None:
        secret = "sk-promptsecret1234567890"
        request = make_request(request_id="redacted-prompt")
        request = replace(
            request,
            task=replace(
                request.task,
                objective=f"Inspect with Authorization: Bearer {secret}",
            ),
        )

        snapshot = PromptBuilder().build(request)

        self.assertNotIn(secret, snapshot.system_prompt)
        self.assertIn("Authorization: Bearer", snapshot.system_prompt)
        self.assertEqual(
            snapshot.prompt_hash,
            hashlib.sha256(snapshot.system_prompt.encode("utf-8")).hexdigest(),
        )

    def test_file_and_command_profiles_use_audited_behavior(self) -> None:
        file_secret = "ghp_1234567890abcdefghij"
        file_output = redact_tool_output(
            "read_workspace_file",
            {"workspace_id": "repo", "path": "config.txt"},
            f"token={file_secret}",
        )
        env_secret = "opaquecredentialvalue1234567890"
        command_output = redact_tool_output(
            "run_workspace_command",
            {"command": "env"},
            f"MY_SERVICE_TOKEN={env_secret}",
        )

        self.assertNotIn(file_secret, file_output)
        self.assertIn("«redacted:ghp_…»", file_output)
        self.assertNotIn(env_secret, command_output)
        self.assertIn("MY_SERVICE_TOKEN=", command_output)
        self.assertEqual(
            redact_tool_output("read_fixture", {"key": "plain"}, "ordinary output"),
            "ordinary output",
        )

    async def test_runtime_never_persists_or_replays_raw_credentials(self) -> None:
        prompt_secret = "sk-promptsecret1234567890"
        tool_secret = "ghp_1234567890abcdefghij"
        completion_secret = "sk-completionsecret1234567890"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.db"
            store = RunStore(database)
            fixture = FixtureReader({tool_secret: f"credential={tool_secret}"})
            registry = ToolRegistry()
            registry.register(fixture.definition())
            provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "secret-call",
                                "read_fixture",
                                {"key": tool_secret},
                            ),
                        )
                    ),
                    ModelResponse(
                        completion=CompletionEnvelope(
                            summary=f"Completed with {completion_secret}",
                        )
                    ),
                ]
            )
            request = make_request(
                request_id="runtime-redaction",
                tool_names=("read_fixture",),
                resource_patterns=("fixture:*",),
            )
            request = replace(
                request,
                task=replace(
                    request.task,
                    objective=f"Inspect with Authorization: Bearer {prompt_secret}",
                ),
            )
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=provider,
                registry=registry,
            )

            result = await service.collect(await service.start(request))

            self.assertNotIn(prompt_secret, str(provider.requests[0].messages))
            self.assertNotIn(tool_secret, str(provider.requests[1].messages))
            self.assertNotIn(completion_secret, result.summary)
            store.close()
            persisted = database.read_bytes()
            for secret in (prompt_secret, tool_secret, completion_secret):
                self.assertNotIn(secret.encode("utf-8"), persisted)


if __name__ == "__main__":
    unittest.main()
