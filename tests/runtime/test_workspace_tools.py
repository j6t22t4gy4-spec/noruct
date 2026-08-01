from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.models import ModelResponse, RunStatus, ToolCall
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry, WorkspaceReadTools
from tests.runtime.helpers import completion, make_request


class WorkspaceToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_file_read_and_traversal_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            (root / "source.py").write_text("value = 1\n", encoding="utf-8")
            outside = Path(directory) / "secret.txt"
            outside.write_text("secret", encoding="utf-8")

            tools = WorkspaceReadTools({"repo": root})
            registry = ToolRegistry()
            for definition in tools.definitions():
                registry.register(definition)
            request = make_request(
                request_id="workspace-valid",
                tool_names=("read_workspace_file",),
                resource_patterns=("workspace:repo:*",),
                workspace_id="repo",
            )
            provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "read-valid",
                                "read_workspace_file",
                                {"workspace_id": "repo", "path": "source.py"},
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Read the source")),
                ]
            )
            store = RunStore()
            service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=registry)
            result = await service.collect(await service.start(request))

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(tools.read_call_count, 1)
            self.assertIn("value = 1", str(provider.requests[1].messages[-1].content))
            store.close()

            denied_provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "read-traversal",
                                "read_workspace_file",
                                {"workspace_id": "repo", "path": "../secret.txt"},
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Recovered from invalid path")),
                ]
            )
            denied_store = RunStore()
            denied_service = NativeEmployeeRuntimeService(
                store=denied_store,
                provider=denied_provider,
                registry=registry,
            )
            denied_request = make_request(
                request_id="workspace-denied",
                tool_names=("read_workspace_file",),
                resource_patterns=("workspace:repo:*",),
                workspace_id="repo",
            )
            denied_result = await denied_service.collect(await denied_service.start(denied_request))

            self.assertEqual(denied_result.status, RunStatus.SUCCEEDED)
            self.assertEqual(tools.read_call_count, 1)
            self.assertIn("INVALID_ARGUMENTS", str(denied_provider.requests[1].messages[-1].content))
            denied_store.close()

            policy_store = RunStore()
            policy_provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "read-policy-denied",
                                "read_workspace_file",
                                {"workspace_id": "repo", "path": "source.py"},
                            ),
                        )
                    )
                ]
            )
            policy_service = NativeEmployeeRuntimeService(
                store=policy_store,
                provider=policy_provider,
                registry=registry,
            )
            policy_request = make_request(
                request_id="workspace-policy-denied",
                tool_names=("read_workspace_file",),
                resource_patterns=("workspace:repo:*",),
                workspace_id="repo",
            )
            policy_request = replace(
                policy_request,
                action_policy=replace(policy_request.action_policy, filesystem_policy="DENY"),
            )

            policy_result = await policy_service.collect(await policy_service.start(policy_request))

            self.assertEqual(policy_result.status, RunStatus.FAILED)
            self.assertEqual(policy_result.failure.category.value, "POLICY")
            self.assertEqual(tools.read_call_count, 1)
            policy_store.close()

    async def test_secret_bearing_environment_file_is_denied_after_workspace_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            (root / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
            (root / ".env.example").write_text("API_KEY=\n", encoding="utf-8")

            tools = WorkspaceReadTools({"repo": root})
            registry = ToolRegistry()
            for definition in tools.definitions():
                registry.register(definition)
            request = make_request(
                request_id="workspace-secret-read-denied",
                tool_names=("read_workspace_file",),
                resource_patterns=("workspace:repo:*",),
                workspace_id="repo",
            )
            provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "read-secret-env",
                                "read_workspace_file",
                                {"workspace_id": "repo", "path": ".env"},
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Used the documented shape instead.")),
                ]
            )
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store, provider=provider, registry=registry
            )
            result = await service.collect(await service.start(request))

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(tools.read_call_count, 0)
            self.assertIn("TOOL_REJECTED", str(provider.requests[1].messages[-1].content))
            self.assertNotIn("API_KEY=secret", str(provider.requests))
            store.close()

    async def test_listing_limit_returns_a_safe_recovery_signal_to_the_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            for index in range(501):
                (root / f"file-{index:03d}.txt").touch()

            tools = WorkspaceReadTools({"repo": root})
            registry = ToolRegistry()
            registry.register(
                next(item for item in tools.definitions() if item.name == "list_workspace_files")
            )
            provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "list-root",
                                "list_workspace_files",
                                {"workspace_id": "repo", "path": "."},
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Use a narrower directory.")),
                ]
            )
            store = RunStore()
            service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=registry)
            request = make_request(
                request_id="workspace-listing-limit-recovery",
                tool_names=("list_workspace_files",),
                resource_patterns=("workspace:repo:*",),
                workspace_id="repo",
            )

            result = await service.collect(await service.start(request))

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            tool_result = str(provider.requests[1].messages[-1].content)
            self.assertIn("TOOL_REJECTED", tool_result)
            self.assertIn("bounded path or output policy", tool_result)
            self.assertNotIn("file-500.txt", tool_result)
            store.close()

    async def test_vendored_workspace_search_runs_inside_the_parent_granted_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            (root / "important.py").write_text("TARGET_VALUE = 42\n", encoding="utf-8")
            (root / "notes.txt").write_text("ordinary text\n", encoding="utf-8")
            tools = WorkspaceReadTools({"repo": root})
            registry = ToolRegistry()
            registry.register(
                next(item for item in tools.definitions() if item.name == "search_workspace_files")
            )
            provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "source-search",
                                "search_workspace_files",
                                {
                                    "workspace_id": "repo",
                                    "path": ".",
                                    "pattern": "TARGET_VALUE",
                                    "target": "content",
                                },
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Found the target source evidence.")),
                ]
            )
            store = RunStore()
            service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=registry)
            request = make_request(
                request_id="vendored-workspace-search",
                tool_names=("search_workspace_files",),
                resource_patterns=("workspace:repo:*",),
                workspace_id="repo",
            )

            result = await service.collect(await service.start(request))

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            tool_result = str(provider.requests[1].messages[-1].content)
            self.assertIn("TARGET_VALUE = 42", tool_result)
            self.assertIn("important.py", tool_result)
            store.close()


if __name__ == "__main__":
    unittest.main()
