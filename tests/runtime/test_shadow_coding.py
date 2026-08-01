from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dynamic_firm.cli import (
    RunCommandConfig,
    _action_policy,
    _resolve_foundation_runtime_python,
    run_goal,
)
from dynamic_firm.coding import (
    APPLY_CHANGE_SET_TOOL,
    ChangeSetCatalog,
    CodingWorkResult,
    RoutedEmployeeExecutionService,
    ShadowCodingEmployeeRuntimeService,
    ShadowWorkspaceError,
    ShadowWorkspaceService,
    ValidationAttempt,
)
from dynamic_firm.runtime.models import (
    ActionPolicy,
    ApprovalDecision,
    CompletionEnvelope,
    EventType,
    ModelResponse,
    RunStatus,
    ToolCall,
    ToolEffect,
    ToolGrant,
    Usage,
    RunLimits,
)
from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.product.routing import InputRoute
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry, WorkspaceTools
from tests.runtime.helpers import make_request


class MutatingWorker:
    def __init__(
        self,
        mutation,
        *,
        usage: Usage | None = None,
        validation_attempts: tuple[ValidationAttempt, ...] = (),
        verification_commands: tuple[str, ...] = (),
    ) -> None:
        self.mutation = mutation
        self.usage = usage or Usage(model_calls=9, input_tokens=11, output_tokens=7)
        self.requests = []
        self.visible_paths: tuple[str, ...] = ()
        self.validation_attempts = validation_attempts
        self.verification_commands = verification_commands

    async def execute(self, request, cancellation):
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        self.visible_paths = tuple(
            sorted(path.relative_to(request.workspace).as_posix() for path in request.workspace.rglob("*"))
        )
        self.mutation(request.workspace)
        return CodingWorkResult(
            summary="Prepared the bounded shadow change.",
            acceptance_evidence=("fake worker completed",),
            validation_attempts=self.validation_attempts,
            verification_commands=self.verification_commands,
            usage=self.usage,
            provider_request_id="fake-shadow-turn",
        )


class RecoveryWorker:
    def __init__(
        self,
        *,
        pass_on_call: int | None,
        second_call_delay: float = 0.0,
        usage: Usage | None = None,
    ) -> None:
        self.pass_on_call = pass_on_call
        self.second_call_delay = second_call_delay
        self.usage = usage or Usage(input_tokens=11, output_tokens=7)
        self.requests = []

    async def execute(self, request, cancellation):
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        if len(self.requests) == 2 and self.second_call_delay:
            await asyncio.sleep(self.second_call_delay)
            cancellation.raise_if_cancelled()
        passed = self.pass_on_call is not None and len(self.requests) >= self.pass_on_call
        (request.workspace / "app.py").write_text(
            "valid\n" if passed else "invalid\n",
            encoding="utf-8",
        )
        return CodingWorkResult(
            summary=f"Prepared shadow candidate {len(self.requests)}.",
            usage=self.usage,
            provider_request_id=f"recovery-call-{len(self.requests)}",
        )


class ExactAppValidator:
    def __init__(self, *, cancel_after_failure: bool = False) -> None:
        self.cancel_after_failure = cancel_after_failure
        self.workspaces = []

    async def validate(self, request, cancellation):
        cancellation.raise_if_cancelled()
        self.workspaces.append(request.workspace)
        passed = (request.workspace / "app.py").read_text(encoding="utf-8") == "valid\n"
        if not passed and self.cancel_after_failure:
            cancellation.cancel("cancelled after first validation")
        return ValidationAttempt(
            "exact-app-contract",
            passed,
            "passed" if passed else "failed:content",
        )


class RecordingApproval:
    def __init__(self, *decisions: ApprovalDecision, before_decision=None) -> None:
        self.decisions = list(decisions) or [ApprovalDecision.DENY]
        self.before_decision = before_decision
        self.requests = []

    async def request(self, request, cancellation):
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        if self.before_decision:
            self.before_decision()
        return self.decisions.pop(0) if self.decisions else ApprovalDecision.DENY


def shadow_request(request_id: str, *, verify_applied_change: bool = False):
    request = make_request(request_id=request_id, workspace_id="repo")
    grants = [
        ToolGrant(
            tool_name=APPLY_CHANGE_SET_TOOL,
            allowed_effects=(ToolEffect.WRITE,),
            resource_patterns=("workspace:repo:change-set:*",),
            max_calls=1,
            requires_approval=True,
        ),
    ]
    if verify_applied_change:
        grants.append(
            ToolGrant(
                tool_name="run_workspace_command",
                allowed_effects=(ToolEffect.EXECUTE,),
                resource_patterns=("workspace:repo:command:*",),
                max_calls=3,
                requires_approval=True,
            )
        )
    return replace(
        request,
        action_policy=ActionPolicy(
            tool_grants=tuple(grants),
            filesystem_policy="WORKSPACE_WRITE",
            sandbox_profile=(
                "host-workspace-approved"
                if verify_applied_change
                else "shadow-workspace-approved"
            ),
        ),
    )


class ShadowWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_excludes_credentials_metadata_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("before\n", encoding="utf-8")
            (root / ".env").write_text("SECRET=value\n", encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("private\n", encoding="utf-8")
            (root / ".codex").mkdir()
            (root / ".codex" / "auth.json").write_text("token\n", encoding="utf-8")
            try:
                (root / "linked.py").symlink_to(root / "app.py")
            except OSError:
                pass
            worker = MutatingWorker(
                lambda shadow: (shadow / "app.py").write_text("after\n", encoding="utf-8")
            )

            outcome = await ShadowWorkspaceService().execute(
                source_root=root,
                workspace_id="repo",
                request=self._work_request(root),
                worker=worker,
                cancellation=CancellationToken(),
            )

            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "before\n")
            self.assertIn("app.py", worker.visible_paths)
            self.assertFalse(any(path.startswith(".git") for path in worker.visible_paths))
            self.assertFalse(any(path.startswith(".codex") for path in worker.visible_paths))
            self.assertNotIn(".env", worker.visible_paths)
            self.assertNotIn("linked.py", worker.visible_paths)
            self.assertIsNotNone(outcome.change_set)
            self.assertEqual(outcome.change_set.files[0].path, "app.py")

    async def test_snapshot_excludes_large_local_runtime_cache(self) -> None:
        """A workspace-local tool cache must not block an unrelated edit."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("before\n", encoding="utf-8")
            cache = root / ".cache" / "codex-runtimes"
            cache.mkdir(parents=True)
            # Deliberately above the regular snapshot file limit.  It is a
            # generated runtime artifact, not user source for the shadow.
            (cache / "runtime.bin").write_bytes(b"x" * 2_000_001)
            worker = MutatingWorker(
                lambda shadow: (shadow / "app.py").write_text("after\n", encoding="utf-8")
            )

            outcome = await ShadowWorkspaceService().execute(
                source_root=root,
                workspace_id="repo",
                request=self._work_request(root),
                worker=worker,
                cancellation=CancellationToken(),
            )

            self.assertIsNotNone(outcome.change_set)
            self.assertIn("app.py", worker.visible_paths)
            self.assertFalse(any(path.startswith(".cache/") for path in worker.visible_paths))

    async def test_deletion_and_binary_changes_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("before\n", encoding="utf-8")
            deleting = MutatingWorker(lambda shadow: (shadow / "app.py").unlink())
            with self.assertRaises(ShadowWorkspaceError) as deleted:
                await ShadowWorkspaceService().execute(
                    source_root=root,
                    workspace_id="repo",
                    request=self._work_request(root),
                    worker=deleting,
                    cancellation=CancellationToken(),
                )
            self.assertEqual(deleted.exception.code, "SHADOW_DELETE_UNSUPPORTED")

            binary = MutatingWorker(lambda shadow: (shadow / "app.py").write_bytes(b"x\x00y"))
            with self.assertRaises(ShadowWorkspaceError) as unsupported:
                await ShadowWorkspaceService().execute(
                    source_root=root,
                    workspace_id="repo",
                    request=self._work_request(root),
                    worker=binary,
                    cancellation=CancellationToken(),
                )
            self.assertEqual(unsupported.exception.code, "SHADOW_CHANGE_UNSUPPORTED")

    @staticmethod
    def _work_request(root: Path):
        from dynamic_firm.coding import CodingWorkRequest

        return CodingWorkRequest(
            task_id="task-1",
            objective="Update app.py",
            acceptance_criteria=("app.py is updated",),
            dependency_context=(),
            workspace=root,
            model_profile="fake",
            max_wall_time_ms=10_000,
        )


class ShadowCodingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_party_validation_passes_without_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("before\n", encoding="utf-8")
            worker = RecoveryWorker(pass_on_call=1)
            validator = ExactAppValidator()
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store, service = self._service(root, worker, approval, validator=validator)

            result = await service.collect(await service.start(shadow_request("shadow-first-pass")))

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(result.usage.model_calls, 1)
            self.assertEqual(len(worker.requests), 1)
            self.assertEqual(len(validator.workspaces), 1)
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "valid\n")
            await service.close()
            store.close()

    async def test_failed_validation_recovers_once_in_the_same_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("before\n", encoding="utf-8")
            worker = RecoveryWorker(pass_on_call=2)
            validator = ExactAppValidator()
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store, service = self._service(root, worker, approval, validator=validator)

            result = await service.collect(await service.start(shadow_request("shadow-recovered")))
            events = store.list_events(result.run_id)
            validation_events = [
                event for event in events if event.type == EventType.VALIDATION_RECORDED
            ]

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(result.usage.model_calls, 2)
            self.assertEqual(len(worker.requests), 2)
            self.assertEqual(worker.requests[0].workspace, worker.requests[1].workspace)
            self.assertEqual(validator.workspaces, [worker.requests[0].workspace] * 2)
            self.assertEqual(worker.requests[0].validation_feedback, ())
            self.assertEqual(
                worker.requests[1].validation_feedback,
                (ValidationAttempt("exact-app-contract", False, "failed:content"),),
            )
            self.assertEqual(
                [event.payload["passed"] for event in validation_events],
                [False, True],
            )
            self.assertEqual(
                [
                    event.payload["candidate_changed_paths"]
                    for event in validation_events
                ],
                [["app.py"], ["app.py"]],
            )
            self.assertEqual(len(approval.requests), 1)
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "valid\n")
            await service.close()
            store.close()

    async def test_second_validation_failure_is_terminal_without_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("before\n", encoding="utf-8")
            worker = RecoveryWorker(pass_on_call=None)
            validator = ExactAppValidator()
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store, service = self._service(root, worker, approval, validator=validator)

            result = await service.collect(await service.start(shadow_request("shadow-failed-twice")))
            events = store.list_events(result.run_id)

            self.assertEqual(result.status, RunStatus.FAILED)
            self.assertEqual(result.failure.code, "CODING_VALIDATION_FAILED")
            self.assertEqual(result.usage.model_calls, 2)
            self.assertEqual(len(worker.requests), 2)
            self.assertEqual(approval.requests, [])
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "before\n")
            self.assertEqual(
                [
                    event.payload["passed"]
                    for event in events
                    if event.type == EventType.VALIDATION_RECORDED
                ],
                [False, False],
            )
            self.assertEqual(
                [
                    event.payload["candidate_changed_paths"]
                    for event in events
                    if event.type == EventType.VALIDATION_RECORDED
                ],
                [["app.py"], ["app.py"]],
            )
            await service.close()
            store.close()

    async def test_cancellation_after_first_validation_preserves_the_failed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("before\n", encoding="utf-8")
            worker = RecoveryWorker(pass_on_call=2)
            validator = ExactAppValidator(cancel_after_failure=True)
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store, service = self._service(root, worker, approval, validator=validator)

            result = await service.collect(await service.start(shadow_request("shadow-cancelled")))
            events = store.list_events(result.run_id)

            self.assertEqual(result.status, RunStatus.CANCELLED)
            self.assertEqual(len(worker.requests), 1)
            self.assertEqual(approval.requests, [])
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "before\n")
            self.assertEqual(
                [
                    event.payload["passed"]
                    for event in events
                    if event.type == EventType.VALIDATION_RECORDED
                ],
                [False],
            )
            await service.close()
            store.close()

    async def test_model_call_budget_blocks_recovery_before_a_second_worker_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("before\n", encoding="utf-8")
            worker = RecoveryWorker(pass_on_call=2)
            validator = ExactAppValidator()
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store, service = self._service(root, worker, approval, validator=validator)
            request = shadow_request("shadow-recovery-budget")
            request = replace(request, limits=replace(request.limits, max_model_calls=1))

            result = await service.collect(await service.start(request))

            self.assertEqual(result.status, RunStatus.BUDGET_EXHAUSTED)
            self.assertEqual(result.usage.model_calls, 1)
            self.assertEqual(len(worker.requests), 1)
            self.assertEqual(approval.requests, [])
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "before\n")
            await service.close()
            store.close()

    async def test_output_token_budget_preserves_exact_recovery_admission_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("before\n", encoding="utf-8")
            worker = RecoveryWorker(
                pass_on_call=2,
                usage=Usage(input_tokens=11, output_tokens=7),
            )
            validator = ExactAppValidator()
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store, service = self._service(root, worker, approval, validator=validator)
            request = shadow_request("shadow-recovery-output-budget")
            request = replace(
                request,
                limits=replace(request.limits, max_output_tokens=7),
            )

            result = await service.collect(await service.start(request))
            terminal = [
                event
                for event in store.list_events(result.run_id)
                if event.type == EventType.RUN_BUDGET_EXHAUSTED
            ]

            self.assertEqual(result.status, RunStatus.BUDGET_EXHAUSTED)
            self.assertEqual(len(worker.requests), 1)
            self.assertEqual(len(terminal), 1)
            self.assertEqual(terminal[0].payload["limit"], "max_output_tokens")
            self.assertEqual(approval.requests, [])
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "before\n")
            await service.close()
            store.close()

    async def test_wall_time_budget_cancels_the_second_call_and_preserves_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("before\n", encoding="utf-8")
            worker = RecoveryWorker(pass_on_call=2, second_call_delay=0.5)
            validator = ExactAppValidator()
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store, service = self._service(root, worker, approval, validator=validator)
            request = shadow_request("shadow-recovery-wall-time")
            request = replace(request, limits=replace(request.limits, max_wall_time_ms=50))

            result = await service.collect(await service.start(request))
            events = store.list_events(result.run_id)

            self.assertEqual(result.status, RunStatus.FAILED)
            self.assertEqual(result.failure.code, "CODING_WORKER_TIMEOUT")
            self.assertEqual(result.usage.model_calls, 1)
            self.assertEqual(len(worker.requests), 2)
            self.assertEqual(approval.requests, [])
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "before\n")
            self.assertEqual(
                [
                    event.payload["passed"]
                    for event in events
                    if event.type == EventType.VALIDATION_RECORDED
                ],
                [False],
            )
            await service.close()
            store.close()

    async def test_reported_usage_limit_stops_before_approval_and_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("before\n", encoding="utf-8")
            worker = MutatingWorker(
                lambda shadow: (shadow / "app.py").write_text("after\n", encoding="utf-8"),
                usage=Usage(input_tokens=101),
            )
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store, service = self._service(root, worker, approval)
            request = shadow_request("shadow-over-budget")
            request = replace(
                request,
                limits=replace(request.limits, max_input_tokens=100),
            )

            result = await service.collect(await service.start(request))

            self.assertEqual(result.status, RunStatus.BUDGET_EXHAUSTED)
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
            self.assertEqual(approval.requests, [])
            await service.close()
            store.close()

    async def test_broad_company_refactor_routes_codex_through_shadow_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("before\n", encoding="utf-8")
            worker = MutatingWorker(
                lambda shadow: (shadow / "app.py").write_text("after\n", encoding="utf-8"),
                verification_commands=("printf verified",),
            )
            approval = RecordingApproval(
                ApprovalDecision.ALLOW_ONCE,
                ApprovalDecision.ALLOW_ONCE,
            )
            config = RunCommandConfig(
                goal="Refactor the architecture across multiple files.",
                workspace=root,
                state_path=root / "runtime.db",
                provider_kind="openai_codex",
                base_url="",
                model="codex-default",
                codex_model=None,
                codex_command="codex",
                api_key_env=None,
                request_timeout_seconds=10,
                permission_mode="ask",
                capability_trust_mode="strict",
                run_limits=RunLimits(
                    max_wall_time_ms=10_000,
                    max_model_calls=4,
                    max_tool_calls=2,
                ),
            )

            result = await run_goal(
                config,
                object(),
                approval_port=approval,
                coding_worker=worker,
            )

            self.assertEqual(result.status.value, "SUCCEEDED")
            self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
            self.assertEqual(len(worker.requests), 1)
            self.assertNotEqual(worker.requests[0].workspace, root)
            self.assertFalse(any(path.startswith("runtime.db") for path in worker.visible_paths))
            self.assertEqual(len(approval.requests), 2)
            self.assertEqual(approval.requests[1].tool_name, "run_workspace_command")

    async def test_small_codex_file_request_uses_host_direct_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = MutatingWorker(lambda shadow: self.fail("shadow worker must not run"))
            provider = ScriptedModelProvider(
                (
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "write-direct-1",
                                "write_workspace_file",
                                {
                                    "workspace_id": "noruct-workspace",
                                    "path": "sample-folder/sample.txt",
                                    "content": "direct host write\n",
                                },
                            ),
                        ),
                        finish_reason="tool_calls",
                    ),
                    ModelResponse(
                        completion=CompletionEnvelope(
                            summary="Created the requested file in the workspace."
                        )
                    ),
                )
            )
            config = RunCommandConfig(
                goal="Create sample-folder/sample.txt with a short greeting.",
                workspace=root,
                state_path=root / "runtime.db",
                provider_kind="openai_codex",
                base_url="",
                model="codex-default",
                codex_model=None,
                codex_command="codex",
                api_key_env=None,
                request_timeout_seconds=10,
                permission_mode="ask",
                capability_trust_mode="strict",
                run_limits=RunLimits(max_model_calls=4, max_tool_calls=4),
                runtime_python=_resolve_foundation_runtime_python(""),
            )
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)

            result = await run_goal(
                config,
                provider,
                approval_port=approval,
                coding_worker=worker,
            )

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(
                (root / "sample-folder" / "sample.txt").read_text(encoding="utf-8"),
                "direct host write\n",
            )
            self.assertEqual(worker.requests, [])
            self.assertEqual([request.tool_name for request in approval.requests], ["write_workspace_file"])

    async def test_small_codex_command_request_exposes_host_command_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = MutatingWorker(lambda shadow: self.fail("shadow worker must not run"))
            provider = ScriptedModelProvider(
                (
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "command-direct-1",
                                "run_workspace_command",
                                {
                                    "workspace_id": "noruct-workspace",
                                    "command": "printf command-ready",
                                },
                            ),
                        ),
                        finish_reason="tool_calls",
                    ),
                    ModelResponse(
                        completion=CompletionEnvelope(
                            summary="Ran the requested workspace command."
                        )
                    ),
                )
            )
            config = RunCommandConfig(
                goal="Run the local command printf command-ready.",
                workspace=root,
                state_path=root / "runtime.db",
                provider_kind="openai_codex",
                base_url="",
                model="codex-default",
                codex_model=None,
                codex_command="codex",
                api_key_env=None,
                request_timeout_seconds=10,
                permission_mode="ask",
                capability_trust_mode="strict",
                run_limits=RunLimits(max_model_calls=4, max_tool_calls=4),
                runtime_python=_resolve_foundation_runtime_python(""),
            )
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)

            result = await run_goal(
                config,
                provider,
                approval_port=approval,
                coding_worker=worker,
            )

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(worker.requests, [])
            self.assertEqual([request.tool_name for request in approval.requests], ["run_workspace_command"])
            self.assertIn("command-ready", str(provider.requests[1].messages[-1].content))

    async def test_direct_conversation_keeps_approval_gated_agent_tools(self) -> None:
        """The lexical route may skip a company graph, never tool disclosure."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = MutatingWorker(lambda shadow: self.fail("shadow worker must not run"))
            provider = ScriptedModelProvider(
                (
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "conversation-command-1",
                                "run_workspace_command",
                                {
                                    "workspace_id": "noruct-workspace",
                                    "command": "printf direct-agent-ready",
                                },
                            ),
                        ),
                        finish_reason="tool_calls",
                    ),
                    ModelResponse(
                        completion=CompletionEnvelope(
                            summary="Ran the requested command after approval."
                        )
                    ),
                )
            )
            config = RunCommandConfig(
                # Intentionally has no action keyword: this emulates an
                # ambiguous/direct route whose competent agent recognizes an
                # action through the actual conversation and tool contract.
                goal="Could you take care of this for me?",
                workspace=root,
                state_path=root / "runtime.db",
                provider_kind="openai_codex",
                base_url="",
                model="codex-default",
                codex_model=None,
                codex_command="codex",
                api_key_env=None,
                request_timeout_seconds=10,
                permission_mode="ask",
                capability_trust_mode="strict",
                run_limits=RunLimits(max_model_calls=4, max_tool_calls=4),
                runtime_python=_resolve_foundation_runtime_python(""),
            )
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)

            result = await run_goal(
                config,
                provider,
                approval_port=approval,
                coding_worker=worker,
                route=InputRoute.CONVERSATION,
            )

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(worker.requests, [])
            self.assertEqual(
                [request.tool_name for request in approval.requests],
                ["run_workspace_command"],
            )
            self.assertIn("direct-agent-ready", str(provider.requests[1].messages[-1].content))
            self.assertIn("Do not create a team", str(provider.requests[0].messages[0].content))

    async def test_real_workspace_changes_only_after_allow_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("before\n", encoding="utf-8")
            worker = MutatingWorker(
                lambda shadow: (shadow / "app.py").write_text("after\n", encoding="utf-8")
            )
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store, service = self._service(root, worker, approval)

            result = await service.collect(await service.start(shadow_request("shadow-allowed")))
            events = store.list_events(result.run_id)

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
            self.assertEqual(result.usage.model_calls, 1)
            self.assertEqual(result.usage.tool_calls, 1)
            self.assertEqual(len(approval.requests), 1)
            self.assertIn("app.py", approval.requests[0].preview)
            self.assertIn(EventType.APPROVAL_REQUIRED, [event.type for event in events])
            self.assertIn(EventType.TOOL_SUCCEEDED, [event.type for event in events])
            await service.close()
            store.close()

    async def test_post_apply_verification_requires_a_second_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("before\n", encoding="utf-8")
            worker = MutatingWorker(
                lambda shadow: (shadow / "app.py").write_text("after\n", encoding="utf-8"),
                verification_commands=("printf verified",),
            )
            approval = RecordingApproval(
                ApprovalDecision.ALLOW_ONCE,
                ApprovalDecision.ALLOW_ONCE,
            )
            store, service = self._service(root, worker, approval)

            result = await service.collect(
                await service.start(
                    shadow_request("shadow-verified", verify_applied_change=True)
                )
            )

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
            self.assertEqual(result.usage.tool_calls, 2)
            self.assertEqual([item.tool_name for item in approval.requests], [
                APPLY_CHANGE_SET_TOOL,
                "run_workspace_command",
            ])
            self.assertIn("Verified applied change with approved command: printf verified", result.acceptance_evidence)
            await service.close()
            store.close()

    async def test_rejected_post_apply_verification_keeps_the_committed_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("before\n", encoding="utf-8")
            worker = MutatingWorker(
                lambda shadow: (shadow / "app.py").write_text("after\n", encoding="utf-8"),
                verification_commands=("printf verified",),
            )
            approval = RecordingApproval(
                ApprovalDecision.ALLOW_ONCE,
                ApprovalDecision.DENY,
            )
            store, service = self._service(root, worker, approval)

            result = await service.collect(
                await service.start(
                    shadow_request("shadow-verification-denied", verify_applied_change=True)
                )
            )

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
            self.assertEqual(result.usage.tool_calls, 2)
            self.assertTrue(any("APPROVAL_DENIED" in item for item in result.unresolved_issues))
            await service.close()
            store.close()

    async def test_failed_final_validation_stops_before_approval_and_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("before\n", encoding="utf-8")
            worker = MutatingWorker(
                lambda shadow: (shadow / "app.py").write_text("invalid\n", encoding="utf-8"),
                validation_attempts=(
                    ValidationAttempt("bounded-test", False, "failed:contract"),
                ),
            )
            approval = RecordingApproval(ApprovalDecision.ALLOW_ONCE)
            store, service = self._service(root, worker, approval)

            result = await service.collect(
                await service.start(shadow_request("shadow-validation-failed"))
            )
            events = store.list_events(result.run_id)

            self.assertEqual(result.status, RunStatus.FAILED)
            self.assertEqual(result.failure.code, "CODING_VALIDATION_FAILED")
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
            self.assertEqual(approval.requests, [])
            self.assertIn(EventType.VALIDATION_RECORDED, [event.type for event in events])
            self.assertNotIn(EventType.APPROVAL_REQUIRED, [event.type for event in events])
            self.assertNotIn(EventType.TOOL_STARTED, [event.type for event in events])
            await service.close()
            store.close()

    async def test_denial_and_base_hash_conflict_never_partially_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("first-before\n", encoding="utf-8")
            second.write_text("second-before\n", encoding="utf-8")

            def mutate(shadow: Path) -> None:
                (shadow / "first.py").write_text("first-after\n", encoding="utf-8")
                (shadow / "second.py").write_text("second-after\n", encoding="utf-8")

            denied_worker = MutatingWorker(mutate)
            denied_store, denied_service = self._service(
                root,
                denied_worker,
                RecordingApproval(ApprovalDecision.DENY),
            )
            denied = await denied_service.collect(
                await denied_service.start(shadow_request("shadow-denied"))
            )
            self.assertEqual(denied.status, RunStatus.FAILED)
            self.assertEqual(first.read_text(encoding="utf-8"), "first-before\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "second-before\n")
            await denied_service.close()
            denied_store.close()

            conflict_worker = MutatingWorker(mutate)
            approval = RecordingApproval(
                ApprovalDecision.ALLOW_ONCE,
                before_decision=lambda: second.write_text("concurrent\n", encoding="utf-8"),
            )
            conflict_store, conflict_service = self._service(root, conflict_worker, approval)
            conflict = await conflict_service.collect(
                await conflict_service.start(shadow_request("shadow-conflict"))
            )
            self.assertEqual(conflict.status, RunStatus.FAILED)
            self.assertEqual(first.read_text(encoding="utf-8"), "first-before\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "concurrent\n")
            await conflict_service.close()
            conflict_store.close()

    @staticmethod
    def _service(root: Path, worker, approval, *, validator=None):
        store = RunStore()
        catalog = ChangeSetCatalog({"repo": root})
        registry = ToolRegistry()
        registry.register(catalog.definition())
        for definition in WorkspaceTools({"repo": root}).definitions():
            registry.register(definition)
        service = ShadowCodingEmployeeRuntimeService(
            store=store,
            worker=worker,
            shadow=ShadowWorkspaceService(),
            catalog=catalog,
            registry=registry,
            validator=validator,
            approval_port=approval,
        )
        return store, service


class RoutedExecutionSelectionTests(unittest.TestCase):
    """The optional shadow lane must not eclipse the host tool runtime."""

    @staticmethod
    def _request(objective: str):
        request = make_request()
        return replace(
            request,
            task=replace(request.task, objective=objective),
            action_policy=ActionPolicy(
                tool_grants=(
                    ToolGrant(
                        tool_name=APPLY_CHANGE_SET_TOOL,
                        allowed_effects=(ToolEffect.WRITE,),
                        requires_approval=True,
                    ),
                )
            ),
        )

    def test_small_file_operation_uses_direct_host_lane(self) -> None:
        router = RoutedEmployeeExecutionService(
            native=object(),  # type: ignore[arg-type]
            shadow_coding=object(),  # type: ignore[arg-type]
        )
        self.assertEqual(
            router._select(self._request("Create a folder and write one README file.")),
            "native",
        )

    def test_explicit_broad_refactor_uses_shadow_lane(self) -> None:
        router = RoutedEmployeeExecutionService(
            native=object(),  # type: ignore[arg-type]
            shadow_coding=object(),  # type: ignore[arg-type]
        )
        self.assertEqual(
            router._select(
                self._request("Refactor the architecture across multiple files.")
            ),
            "shadow",
        )

    def test_explicit_shadow_sandbox_profile_overrides_wording_heuristic(self) -> None:
        router = RoutedEmployeeExecutionService(
            native=object(),  # type: ignore[arg-type]
            shadow_coding=object(),  # type: ignore[arg-type]
        )
        request = self._request("Implement the smallest safe correction.")
        request = replace(
            request,
            action_policy=replace(
                request.action_policy,
                sandbox_profile="shadow-workspace-approved",
            ),
        )

        self.assertEqual(router._select(request), "shadow")

    def test_host_direct_only_never_stages_operator_home(self) -> None:
        router = RoutedEmployeeExecutionService(
            native=object(),  # type: ignore[arg-type]
            shadow_coding=object(),  # type: ignore[arg-type]
            host_direct_only=True,
        )
        self.assertEqual(
            router._select(
                self._request("Refactor the architecture across multiple files.")
            ),
            "native",
        )

    def test_codex_ask_policy_keeps_direct_host_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RunCommandConfig(
                goal="Create a folder.",
                workspace=root,
                state_path=root / "runtime.db",
                provider_kind="openai_codex",
                base_url="",
                model="codex-default",
                codex_model=None,
                codex_command="codex",
                api_key_env=None,
                request_timeout_seconds=10,
                permission_mode="ask",
                run_limits=RunLimits(),
            )
            grants = {grant.tool_name for grant in _action_policy(config).tool_grants}

        self.assertTrue(
            {
                APPLY_CHANGE_SET_TOOL,
                "write_workspace_file",
                "edit_workspace_file",
                "patch_workspace_file",
                "run_workspace_command",
            }.issubset(grants)
        )


if __name__ == "__main__":
    unittest.main()
