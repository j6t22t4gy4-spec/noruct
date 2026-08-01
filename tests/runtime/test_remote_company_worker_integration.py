from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.kernel.models import EmployeeRecord, JobLimits, JobStatus
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.product.execution_environment import (
    transfer_workspace_snapshot,
    write_workspace_snapshot_manifest,
)
from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.job_ledger import ActiveJobAuditStatus, ActiveJobInspector, SQLiteActiveJobLedger
from dynamic_firm.runtime.models import (
    ActionPolicy,
    ApprovalDecision,
    EventType,
    ModelResponse,
    ToolCall,
    ToolEffect,
    ToolGrant,
)
from dynamic_firm.runtime.remote_workspace import RemoteWorkspaceTools, remote_worker_config_from_settings
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry
from tests.kernel.helpers import company_request, task
from tests.runtime.helpers import completion


class _Approval:
    def __init__(self) -> None:
        self.requests = []

    async def request(self, request, cancellation):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return ApprovalDecision.ALLOW_ONCE


class _CompletedSshProcess:
    pid = None
    returncode = 0

    async def communicate(self):  # type: ignore[no-untyped-def]
        return b"remote tests passed\n", b""

    async def wait(self):  # type: ignore[no-untyped-def]
        return self.returncode


class _BlockingSshProcess:
    pid = None
    returncode = None

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def communicate(self):  # type: ignore[no-untyped-def]
        self.started.set()
        await asyncio.Event().wait()
        return b"", b""

    def terminate(self) -> None:
        self.returncode = -15

    async def wait(self):  # type: ignore[no-untyped-def]
        return self.returncode


class RemoteCompanyWorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _verified_worker(self, root: Path):
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "test_target.py").write_text("assert True\n", encoding="utf-8")
        manifest = root / "snapshot.json"
        snapshot = write_workspace_snapshot_manifest(workspace=workspace, output_path=manifest)

        def transfer_runner(command, **kwargs):  # type: ignore[no-untyped-def]
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("ForwardAgent=no", command)
            self.assertTrue(kwargs["input"].startswith(b"\x1f\x8b"))
            return subprocess.CompletedProcess(
                command,
                0,
                "test_target.py: OK\nnoruct-transfer-ok:" + snapshot.snapshot_sha256,
                "",
            )

        with patch("dynamic_firm.product.execution_environment.shutil.which", return_value="/usr/bin/ssh"):
            transferred = transfer_workspace_snapshot(
                workspace=workspace,
                snapshot_manifest=manifest,
                host="build.example.test",
                user="operator",
                remote_workspace="/srv/company",
                runner=transfer_runner,
            )
        receipt = root / "verified-transfer-receipt.json"
        receipt.write_text(json.dumps(transferred.to_dict()), encoding="utf-8")
        worker = remote_worker_config_from_settings(
            {"remote_worker": {
                "enabled": True,
                "target_id": "build",
                "receipt": str(receipt),
                "programs": {"tests": "/usr/bin/pytest"},
            }}
        )
        assert worker is not None
        return worker

    def _request(self, worker, *, wall_time_ms: int = 5_000):
        request = company_request(
            (task("final", capabilities=("analysis",)),),
            final_task_id="final",
            roster=(EmployeeRecord("operator", "Operator", ("analysis",)),),
            limits=JobLimits(max_wall_time_ms=wall_time_ms),
        )
        return replace(
            request,
            action_policy=ActionPolicy(
                tool_grants=(ToolGrant(
                    tool_name="run_remote_workspace_program",
                    allowed_effects=(ToolEffect.EXECUTE,),
                    resource_patterns=(
                        f"remote-workspace:{worker.target_id}:*:{worker.snapshot_sha256}",
                    ),
                    max_calls=1,
                    requires_approval=True,
                ),),
                sandbox_profile="remote-workspace-approved",
            ),
        )

    async def test_verified_transfer_then_company_tool_approval_execution_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = self._verified_worker(root)
            registry = ToolRegistry()
            registry.register(RemoteWorkspaceTools(worker).definition())
            approval = _Approval()
            provider = ScriptedModelProvider([
                ModelResponse(tool_calls=(ToolCall(
                    "remote-tests", "run_remote_workspace_program",
                    {"program_id": "tests", "arguments": ["-q"]},
                ),)),
                ModelResponse(completion=completion("Remote tests are complete.")),
            ])
            store = RunStore(root / "runtime.db")
            service = NativeEmployeeRuntimeService(
                store=store, provider=provider, registry=registry, approval_port=approval,
            )
            captured: list[tuple[object, ...]] = []

            async def spawn(*command, **kwargs):  # type: ignore[no-untyped-def]
                captured.append(command)
                self.assertIn("StrictHostKeyChecking=yes", command)
                self.assertIn("ForwardAgent=no", command)
                self.assertIn("ClearAllForwardings=yes", command)
                self.assertIn("RequestTTY=no", command)
                self.assertTrue(str(command[-1]).startswith("cd -- "))
                return _CompletedSshProcess()

            request = self._request(worker)
            with patch("dynamic_firm.runtime.remote_workspace.asyncio.create_subprocess_exec", spawn):
                result = await FirmKernel(
                    employee_execution=service,
                    active_job_ledger=SQLiteActiveJobLedger(store),
                ).run(request)
            runs = store.list_job_runs(request.job_id)
            events = store.list_events(runs[0]["run_id"])
            audit = ActiveJobInspector(store).inspect(request.job_id)
            await service.close()
            store.close()

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(len(captured), 1)
        self.assertEqual(len(approval.requests), 1)
        self.assertIn("Remote build/tests", approval.requests[0].preview)
        event_types = [item.type for item in events]
        self.assertLess(event_types.index(EventType.APPROVAL_REQUIRED), event_types.index(EventType.TOOL_STARTED))
        self.assertIn(EventType.APPROVAL_RESOLVED, event_types)
        self.assertIn(EventType.TOOL_SUCCEEDED, event_types)
        self.assertEqual(audit.audit_status, ActiveJobAuditStatus.TERMINAL)
        self.assertTrue(audit.replay_matches)
        self.assertEqual(audit.attempt_count, 1)

    async def test_company_wall_deadline_cancels_the_remote_process_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = self._verified_worker(root)
            registry = ToolRegistry()
            registry.register(RemoteWorkspaceTools(worker).definition())
            process = _BlockingSshProcess()
            service = NativeEmployeeRuntimeService(
                store=RunStore(root / "runtime.db"),
                provider=ScriptedModelProvider([ModelResponse(tool_calls=(ToolCall(
                    "remote-tests", "run_remote_workspace_program",
                    {"program_id": "tests", "arguments": []},
                ),))]),
                registry=registry,
                approval_port=_Approval(),
            )
            store = service.store

            async def spawn(*command, **kwargs):  # type: ignore[no-untyped-def]
                return process

            request = self._request(worker, wall_time_ms=150)
            with patch("dynamic_firm.runtime.remote_workspace.asyncio.create_subprocess_exec", spawn):
                result = await FirmKernel(
                    employee_execution=service,
                    active_job_ledger=SQLiteActiveJobLedger(store),
                ).run(request)
            audit = ActiveJobInspector(store).inspect(request.job_id)
            events = store.list_events(store.list_job_runs(request.job_id)[0]["run_id"])
            await service.close()
            store.close()

        self.assertTrue(process.started.is_set())
        self.assertEqual(process.returncode, -15)
        self.assertEqual(result.status, JobStatus.BUDGET_EXHAUSTED)
        self.assertEqual(result.metrics.task_mutation_count, 0)
        self.assertEqual(audit.audit_status, ActiveJobAuditStatus.TERMINAL)
        event_types = [item.type for item in events]
        self.assertIn(EventType.TOOL_EFFECT_OUTCOME_UNKNOWN, event_types)
        self.assertNotIn(EventType.TOOL_FAILED, event_types)
