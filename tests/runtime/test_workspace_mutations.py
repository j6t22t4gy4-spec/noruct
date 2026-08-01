from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.models import (
    ActionPolicy,
    ApprovalDecision,
    EventType,
    ModelResponse,
    RunStatus,
    ToolCall,
    ToolEffect,
    ToolGrant,
)
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry, ToolValidationError, WorkspaceTools
from tests.runtime.helpers import completion, make_request


class RecordingApproval:
    def __init__(self, *decisions: ApprovalDecision) -> None:
        self.decisions = list(decisions)
        self.requests = []

    async def request(self, request, cancellation):
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        return self.decisions.pop(0) if self.decisions else ApprovalDecision.DENY


class FailingApproval:
    async def request(self, request, cancellation):
        cancellation.raise_if_cancelled()
        raise RuntimeError("terminal redraw failed")


def mutation_request(request_id: str, *tool_names: str):
    effects = {
        "write_workspace_file": ToolEffect.WRITE,
        "edit_workspace_file": ToolEffect.WRITE,
        "patch_workspace_file": ToolEffect.WRITE,
        "apply_workspace_multi_patch": ToolEffect.WRITE,
        "move_workspace_file": ToolEffect.WRITE,
        "delete_workspace_file": ToolEffect.WRITE,
        "run_workspace_command": ToolEffect.EXECUTE,
        "run_workspace_background_command": ToolEffect.EXECUTE,
        "write_workspace_process_stdin": ToolEffect.EXECUTE,
        "stop_workspace_process": ToolEffect.EXECUTE,
    }
    request = make_request(
        request_id=request_id,
        workspace_id="repo",
        limits=replace(make_request().limits, max_consecutive_errors=3),
    )
    return replace(
        request,
        action_policy=ActionPolicy(
            tool_grants=tuple(
                ToolGrant(
                    tool_name=name,
                    allowed_effects=(effects[name],),
                    resource_patterns=("workspace:repo:*",),
                    max_calls=8,
                    requires_approval=True,
                )
                for name in tool_names
            ),
            filesystem_policy="WORKSPACE_WRITE",
            sandbox_profile="host-workspace-approved",
        ),
    )


class WorkspaceMutationTests(unittest.IsolatedAsyncioTestCase):
    async def test_trusted_policy_runs_an_already_granted_workspace_write_without_dialog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ToolRegistry()
            for definition in WorkspaceTools({"repo": root}).definitions():
                registry.register(definition)
            provider = ScriptedModelProvider(
                [
                    ModelResponse(tool_calls=(ToolCall(
                        "trusted-write", "write_workspace_file",
                        {"workspace_id": "repo", "path": "trusted.txt", "content": "recorded\n"},
                    ),)),
                    ModelResponse(completion=completion("Implemented the file")),
                ]
            )
            base = mutation_request("trusted-write", "write_workspace_file")
            policy = replace(
                base.action_policy,
                capability_trust_mode="trusted",
                auto_approved_tool_names=("write_workspace_file",),
            )
            approval = RecordingApproval(ApprovalDecision.DENY)
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store, provider=provider, registry=registry, approval_port=approval
            )

            result = await service.collect(
                await service.start(replace(base, action_policy=policy))
            )

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual((root / "trusted.txt").read_text(encoding="utf-8"), "recorded\n")
            self.assertEqual(approval.requests, [])
            events = store.list_events(result.run_id)
            self.assertIn(EventType.TOOL_STARTED, [event.type for event in events])
            self.assertNotIn(EventType.APPROVAL_REQUIRED, [event.type for event in events])
            await service.close()
            store.close()

    async def test_write_waits_for_approval_and_records_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = WorkspaceTools({"repo": root})
            registry = ToolRegistry()
            for definition in tools.definitions():
                registry.register(definition)
            provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "write-1",
                                "write_workspace_file",
                                {"workspace_id": "repo", "path": "src/app.py", "content": "value = 1\n"},
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Implemented the file")),
                ]
            )
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=provider,
                registry=registry,
                approval_port=approval,
            )

            result = await service.collect(
                await service.start(mutation_request("approved-write", "write_workspace_file"))
            )
            events = store.list_events(result.run_id)

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual((root / "src" / "app.py").read_text(encoding="utf-8"), "value = 1\n")
            self.assertEqual(len(approval.requests), 1)
            self.assertEqual(approval.requests[0].preview, "Write src/app.py (10 bytes)")
            self.assertLess(
                next(i for i, event in enumerate(events) if event.type == EventType.APPROVAL_REQUIRED),
                next(i for i, event in enumerate(events) if event.type == EventType.TOOL_STARTED),
            )
            self.assertIn(EventType.APPROVAL_RESOLVED, [event.type for event in events])
            await service.close()
            store.close()

    async def test_rejected_write_is_returned_to_model_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("before\n", encoding="utf-8")
            tools = WorkspaceTools({"repo": root})
            registry = ToolRegistry()
            for definition in tools.definitions():
                registry.register(definition)
            provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "write-denied",
                                "write_workspace_file",
                                {"workspace_id": "repo", "path": "app.py", "content": "after\n"},
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("The user kept the original file")),
                ]
            )
            approval = RecordingApproval(ApprovalDecision.DENY)
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=provider,
                registry=registry,
                approval_port=approval,
            )

            result = await service.collect(
                await service.start(mutation_request("denied-write", "write_workspace_file"))
            )

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
            self.assertIn("APPROVAL_DENIED", str(provider.requests[1].messages[-1].content))
            await service.close()
            store.close()

    async def test_unavailable_approval_is_not_recorded_as_a_user_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("before\n", encoding="utf-8")
            registry = ToolRegistry()
            for definition in WorkspaceTools({"repo": root}).definitions():
                registry.register(definition)
            provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "write-unavailable",
                                "write_workspace_file",
                                {
                                    "workspace_id": "repo",
                                    "path": "app.py",
                                    "content": "after\n",
                                },
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Approval surface was unavailable")),
                ]
            )
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=provider,
                registry=registry,
                approval_port=FailingApproval(),
            )

            result = await service.collect(
                await service.start(mutation_request("unavailable-write", "write_workspace_file"))
            )
            approvals = [
                event
                for event in store.list_events(result.run_id)
                if event.type == EventType.APPROVAL_RESOLVED
            ]

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
            self.assertIn("APPROVAL_UNAVAILABLE", str(provider.requests[1].messages[-1].content))
            self.assertEqual(approvals[0].payload["decision"], ApprovalDecision.UNAVAILABLE.value)
            await service.close()
            store.close()

    async def test_edit_requires_one_unique_original_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("same\nsame\n", encoding="utf-8")
            tools = WorkspaceTools({"repo": root})
            registry = ToolRegistry()
            for definition in tools.definitions():
                registry.register(definition)
            provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "edit-ambiguous",
                                "edit_workspace_file",
                                {
                                    "workspace_id": "repo",
                                    "path": "app.py",
                                    "old_text": "same",
                                    "new_text": "changed",
                                },
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Ambiguous edit was not applied")),
                ]
            )
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=provider,
                registry=registry,
                approval_port=approval,
            )

            result = await service.collect(
                await service.start(mutation_request("ambiguous-edit", "edit_workspace_file"))
            )

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(target.read_text(encoding="utf-8"), "same\nsame\n")
            self.assertEqual(len(approval.requests), 1)
            self.assertIn("TOOL_REJECTED", str(provider.requests[1].messages[-1].content))
            await service.close()
            store.close()

    async def test_source_patch_uses_approval_and_applies_a_fuzzy_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8")
            registry = ToolRegistry()
            for definition in WorkspaceTools({"repo": root}).definitions():
                registry.register(definition)
            provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "patch-fuzzy",
                                "patch_workspace_file",
                                {
                                    "workspace_id": "repo",
                                    "path": "app.py",
                                    "old_text": "value=1",
                                    "new_text": "value = 2",
                                },
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Source patch completed")),
                ]
            )
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=provider,
                registry=registry,
                approval_port=approval,
            )

            result = await service.collect(
                await service.start(mutation_request("source-patch", "patch_workspace_file"))
            )

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual(len(approval.requests), 1)
            self.assertEqual(approval.requests[0].tool_name, "patch_workspace_file")
            self.assertIn("files_modified", str(provider.requests[1].messages[-1].content))
            await service.close()
            store.close()

    async def test_source_multi_patch_preflights_all_paths_then_applies_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8")
            registry = ToolRegistry()
            for definition in WorkspaceTools({"repo": root}).definitions():
                registry.register(definition)
            patch = """*** Begin Patch
*** Update File: app.py
@@
-value = 1
+value = 2
*** Add File: note.txt
+ready
*** End Patch"""
            provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "multi-patch",
                                "apply_workspace_multi_patch",
                                {"workspace_id": "repo", "patch": patch},
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Source multi-file patch completed")),
                ]
            )
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=provider,
                registry=registry,
                approval_port=approval,
            )

            result = await service.collect(
                await service.start(
                    mutation_request("source-multi-patch", "apply_workspace_multi_patch")
                )
            )

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "ready")
            self.assertEqual(len(approval.requests), 1)
            self.assertEqual(approval.requests[0].tool_name, "apply_workspace_multi_patch")
            self.assertIn("Apply multi-file patch (2 operation(s))", approval.requests[0].preview)
            self.assertIn("files_created", str(provider.requests[1].messages[-1].content))
            await service.close()
            store.close()

    async def test_source_multi_patch_rejects_unsafe_path_before_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("before\n", encoding="utf-8")
            registry = ToolRegistry()
            for definition in WorkspaceTools({"repo": root}).definitions():
                registry.register(definition)
            patch = """*** Begin Patch
*** Update File: ../outside.txt
+not allowed
*** End Patch"""
            provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "unsafe-multi-patch",
                                "apply_workspace_multi_patch",
                                {"workspace_id": "repo", "patch": patch},
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Unsafe patch was rejected")),
                ]
            )
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=provider,
                registry=registry,
                approval_port=approval,
            )

            result = await service.collect(
                await service.start(
                    mutation_request("unsafe-multi-patch", "apply_workspace_multi_patch")
                )
            )

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
            self.assertFalse((root.parent / "outside.txt").exists())
            self.assertEqual(approval.requests, [])
            self.assertIn("INVALID_ARGUMENTS", str(provider.requests[1].messages[-1].content))
            await service.close()
            store.close()

    async def test_source_multi_patch_moves_and_deletes_after_one_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "move-me.txt").write_text("move\n", encoding="utf-8")
            (root / "delete-me.txt").write_text("delete\n", encoding="utf-8")
            registry = ToolRegistry()
            for definition in WorkspaceTools({"repo": root}).definitions():
                registry.register(definition)
            patch = """*** Begin Patch
*** Move File: move-me.txt -> moved.txt
*** Delete File: delete-me.txt
*** End Patch"""
            provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "move-delete-multi-patch",
                                "apply_workspace_multi_patch",
                                {"workspace_id": "repo", "patch": patch},
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Source move and delete completed")),
                ]
            )
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=provider,
                registry=registry,
                approval_port=approval,
            )

            result = await service.collect(
                await service.start(
                    mutation_request("move-delete-multi-patch", "apply_workspace_multi_patch")
                )
            )

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertFalse((root / "move-me.txt").exists())
            self.assertEqual((root / "moved.txt").read_text(encoding="utf-8"), "move\n")
            self.assertFalse((root / "delete-me.txt").exists())
            self.assertEqual(len(approval.requests), 1)
            self.assertIn("move move-me.txt → moved.txt", approval.requests[0].preview)
            self.assertIn("delete delete-me.txt", approval.requests[0].preview)
            await service.close()
            store.close()

    async def test_direct_source_move_and_delete_use_separate_approvals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "old.txt").write_text("move\n", encoding="utf-8")
            (root / "remove.txt").write_text("remove\n", encoding="utf-8")
            registry = ToolRegistry()
            for definition in WorkspaceTools({"repo": root}).definitions():
                registry.register(definition)
            provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "direct-move",
                                "move_workspace_file",
                                {
                                    "workspace_id": "repo",
                                    "source_path": "old.txt",
                                    "destination_path": "new.txt",
                                },
                            ),
                            ToolCall(
                                "direct-delete",
                                "delete_workspace_file",
                                {"workspace_id": "repo", "path": "remove.txt"},
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Direct source file actions completed")),
                ]
            )
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALLOW_ONCE)
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=provider,
                registry=registry,
                approval_port=approval,
            )

            result = await service.collect(
                await service.start(
                    mutation_request(
                        "direct-source-file-actions",
                        "move_workspace_file",
                        "delete_workspace_file",
                    )
                )
            )

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertFalse((root / "old.txt").exists())
            self.assertEqual((root / "new.txt").read_text(encoding="utf-8"), "move\n")
            self.assertFalse((root / "remove.txt").exists())
            self.assertEqual(
                [request.preview for request in approval.requests],
                ["Move old.txt → new.txt", "Delete remove.txt"],
            )
            await service.close()
            store.close()

    async def test_command_has_bounded_clean_environment_and_destructive_input_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = WorkspaceTools(
                {"repo": root},
                environ={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": "/secret/user/home",
                    "SECRET_TOKEN": "must-not-leak",
                },
            )
            registry = ToolRegistry()
            for definition in tools.definitions():
                registry.register(definition)
            provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "command-clean",
                                "run_workspace_command",
                                {
                                    "workspace_id": "repo",
                                    "command": "env",
                                },
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Command completed without secret access")),
                ]
            )
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=provider,
                registry=registry,
                approval_port=approval,
            )

            result = await service.collect(
                await service.start(mutation_request("clean-command", "run_workspace_command"))
            )

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertNotIn("must-not-leak", str(provider.requests[1].messages[-1].content))
            self.assertNotIn("/secret/user/home", str(provider.requests[1].messages[-1].content))
            self.assertIn(str(root), str(provider.requests[1].messages[-1].content))
            definition = registry.get("run_workspace_command")
            with self.assertRaisesRegex(Exception, "destructive file deletion"):
                definition.validator(
                    {"workspace_id": "repo", "command": "rm -rf build"}
                )
            await service.close()
            store.close()

    async def test_background_process_lifecycle_preserves_approval_and_workspace_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = WorkspaceTools({"repo": root})
            registry = ToolRegistry()
            for definition in tools.definitions():
                registry.register(definition)
            start_provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "background-start",
                                "run_workspace_background_command",
                                {
                                    "workspace_id": "repo",
                                    "command": "printf 'ready\\n'; sleep 30",
                                },
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Background task started")),
                ]
            )
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALLOW_ONCE)
            start_store = RunStore()
            start_service = NativeEmployeeRuntimeService(
                store=start_store,
                provider=start_provider,
                registry=registry,
                approval_port=approval,
            )
            start_result = await start_service.collect(
                await start_service.start(
                    mutation_request("background-start", "run_workspace_background_command")
                )
            )
            workspace_key = str(root.resolve())
            processes = tools.background_registry.list(workspace_key=workspace_key)
            self.assertEqual(start_result.status, RunStatus.SUCCEEDED)
            self.assertEqual(len(processes), 1)
            process_id = str(processes[0]["process_id"])
            self.assertEqual(approval.requests[0].tool_name, "run_workspace_background_command")
            other_root = root / "other-workspace"
            other_root.mkdir()
            other_tools = WorkspaceTools({"repo": other_root})
            other_inspect = next(
                item for item in other_tools.definitions() if item.name == "inspect_workspace_process"
            )
            with self.assertRaises(ToolValidationError):
                other_inspect.validator({"workspace_id": "repo", "process_id": process_id})

            inspect_provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "background-inspect",
                                "inspect_workspace_process",
                                {"workspace_id": "repo", "process_id": process_id},
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Observed the background task")),
                ]
            )
            inspect_store = RunStore()
            inspect_service = NativeEmployeeRuntimeService(
                store=inspect_store,
                provider=inspect_provider,
                registry=registry,
            )
            inspect_result = await inspect_service.collect(
                await inspect_service.start(
                    make_request(
                        request_id="background-inspect",
                        tool_names=("inspect_workspace_process",),
                        resource_patterns=("workspace:repo:*",),
                        workspace_id="repo",
                    )
                )
            )
            self.assertEqual(inspect_result.status, RunStatus.SUCCEEDED)
            self.assertIn("ready", str(inspect_provider.requests[1].messages[-1].content))

            stop_provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "background-stop",
                                "stop_workspace_process",
                                {"workspace_id": "repo", "process_id": process_id},
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Background task stopped")),
                ]
            )
            stop_store = RunStore()
            stop_service = NativeEmployeeRuntimeService(
                store=stop_store,
                provider=stop_provider,
                registry=registry,
                approval_port=approval,
            )
            stop_result = await stop_service.collect(
                await stop_service.start(
                    mutation_request("background-stop", "stop_workspace_process")
                )
            )
            stopped = tools.background_registry.inspect(
                workspace_key=workspace_key, process_id=process_id, include_output=True
            )
            self.assertEqual(stop_result.status, RunStatus.SUCCEEDED)
            self.assertEqual(stopped["completion_reason"], "stopped")
            self.assertEqual(approval.requests[1].tool_name, "stop_workspace_process")
            await start_service.close()
            await inspect_service.close()
            await stop_service.close()
            start_store.close()
            inspect_store.close()
            stop_store.close()

    @unittest.skipUnless(os.name == "posix", "PTY process test requires a POSIX terminal")
    async def test_interactive_workspace_process_requires_separate_input_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = WorkspaceTools({"repo": root})
            registry = ToolRegistry()
            for definition in tools.definitions():
                registry.register(definition)
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALLOW_ONCE)

            start_provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "pty-start",
                                "run_workspace_background_command",
                                {
                                    "workspace_id": "repo",
                                    "command": "IFS= read -r line; printf 'ack:%s\\n' \"$line\"",
                                    "interactive": True,
                                },
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Interactive process started")),
                ]
            )
            start_store = RunStore()
            start_service = NativeEmployeeRuntimeService(
                store=start_store,
                provider=start_provider,
                registry=registry,
                approval_port=approval,
            )
            start_result = await start_service.collect(
                await start_service.start(
                    mutation_request("pty-start", "run_workspace_background_command")
                )
            )
            process = tools.background_registry.list(workspace_key=str(root.resolve()))[0]
            process_id = str(process["process_id"])
            self.assertTrue(process["interactive"])
            self.assertEqual(start_result.status, RunStatus.SUCCEEDED)

            input_provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "pty-input",
                                "write_workspace_process_stdin",
                                {"workspace_id": "repo", "process_id": process_id, "data": "hello" + chr(10)},
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Interactive input sent")),
                ]
            )
            input_store = RunStore()
            input_service = NativeEmployeeRuntimeService(
                store=input_store,
                provider=input_provider,
                registry=registry,
                approval_port=approval,
            )
            input_result = await input_service.collect(
                await input_service.start(
                    mutation_request("pty-input", "write_workspace_process_stdin")
                )
            )
            self.assertEqual(input_result.status, RunStatus.SUCCEEDED)
            self.assertEqual(
                [request.tool_name for request in approval.requests],
                ["run_workspace_background_command", "write_workspace_process_stdin"],
            )

            wait_provider = ScriptedModelProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "pty-wait",
                                "wait_workspace_process",
                                {"workspace_id": "repo", "process_id": process_id, "timeout_seconds": 5},
                            ),
                        )
                    ),
                    ModelResponse(completion=completion("Interactive process completed")),
                ]
            )
            wait_store = RunStore()
            wait_service = NativeEmployeeRuntimeService(
                store=wait_store,
                provider=wait_provider,
                registry=registry,
            )
            wait_result = await wait_service.collect(
                await wait_service.start(
                    make_request(
                        request_id="pty-wait",
                        tool_names=("wait_workspace_process",),
                        resource_patterns=("workspace:repo:*",),
                        workspace_id="repo",
                    )
                )
            )
            self.assertEqual(wait_result.status, RunStatus.SUCCEEDED)
            self.assertIn("ack:hello", str(wait_provider.requests[1].messages[-1].content))
            await start_service.close()
            await input_service.close()
            await wait_service.close()
            start_store.close()
            input_store.close()
            wait_store.close()


if __name__ == "__main__":
    unittest.main()
