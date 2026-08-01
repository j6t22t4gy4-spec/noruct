"""Quota-free Employee Runtime foundation contract matrix."""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

from dynamic_firm.company.store import CompanyStateStore
from dynamic_firm.evaluation.product_preview_contract import product_preview_contract
from dynamic_firm.kernel import (
    CompanyRunRequest,
    EmployeeRecord,
    FirmKernel,
    JobLimits,
    JobStatus,
    JobTask,
    PlanProposal,
    TaskMutationType,
)
from dynamic_firm.mcp_connector import McpReadOnlyConfig
from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.models import (
    ActionPolicy, ApprovalDecision, CompletionEnvelope, ContextBundle,
    EmployeeRunRequest, EmployeeSnapshot, EventType, ModelResponse,
    RunLimits, RunSignal, RunStatus, SignalCode, TaskEnvelope, ToolCall,
    ToolEffect, ToolGrant,
)
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import (
    FixtureReader,
    IdempotencyMode,
    ToolDefinition,
    ToolRegistry,
    ToolRisk,
    WorkspaceTools,
)

from .runtime import NoructEmployeeRuntimeService



class _AllowOnce:
    async def request(self, request, cancellation: CancellationToken) -> ApprovalDecision:
        cancellation.raise_if_cancelled()
        return ApprovalDecision.ALLOW_ONCE


class _Deny:
    async def request(self, request, cancellation: CancellationToken) -> ApprovalDecision:
        cancellation.raise_if_cancelled()
        return ApprovalDecision.DENY


def _request(
    request_id: str,
    *,
    grants: tuple[ToolGrant, ...] = (),
    filesystem_policy: str = "READ_ONLY",
) -> EmployeeRunRequest:
    return EmployeeRunRequest(
        request_id=request_id,
        employee=EmployeeSnapshot("employee-parity", "Parity Analyst", ("repository_analysis",)),
        task=TaskEnvelope("job-parity", 1, request_id, 1, "Verify the bounded preview contract.", ("repository_analysis",), ("Return evidence.",)),
        context=ContextBundle(company_policy_excerpt="No unapproved external effects."),
        limits=RunLimits(max_model_calls=3, max_tool_calls=1, max_wall_time_ms=15_000),
        action_policy=ActionPolicy(tool_grants=grants, filesystem_policy=filesystem_policy),
    )


async def run_preview_parity(*, python_executable: str) -> dict[str, Any]:
    """Exercise direct, parent-tool, approval and cancel contracts offline."""

    scenarios: dict[str, dict[str, Any]] = {}

    async def execute(name: str, provider, registry, request, approval=None) -> tuple[object, list[object]]:
        store = RunStore()
        service = NoructEmployeeRuntimeService(store=store, provider=provider, registry=registry, approval_port=approval, python_executable=python_executable)
        try:
            handle = await service.start(request)
            result = await service.collect(handle)
            return result, store.list_events(handle.run_id)
        finally:
            await service.close()
            store.close()

    direct_provider = ScriptedModelProvider([ModelResponse(completion=CompletionEnvelope(summary="direct"))])
    direct, direct_events = await execute("direct", direct_provider, ToolRegistry(), _request("parity-direct"))
    scenarios["direct"] = {"passed": direct.status == RunStatus.SUCCEEDED and direct_provider.call_count == 1, "model_calls": direct_provider.call_count, "tool_intents": sum(event.type == EventType.TOOL_INTENT_RECORDED for event in direct_events)}

    registry = ToolRegistry(); fixture = FixtureReader({"answer": "42"}); registry.register(fixture.definition())
    tool_provider = ScriptedModelProvider([ModelResponse(tool_calls=(ToolCall("read-1", "read_fixture", {"key": "answer"}),), finish_reason="tool_calls"), ModelResponse(completion=CompletionEnvelope(summary="tool"))])
    grant = ToolGrant("read_fixture", (ToolEffect.READ,), ("fixture:*",), max_calls=1)
    tool, tool_events = await execute("tool", tool_provider, registry, _request("parity-tool", grants=(grant,)))
    scenarios["parent_tool"] = {"passed": tool.status == RunStatus.SUCCEEDED and fixture.call_count == 1 and tool.usage.tool_calls == 1, "model_calls": tool_provider.call_count, "tool_intents": sum(event.type == EventType.TOOL_INTENT_RECORDED for event in tool_events)}

    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory); registry = ToolRegistry()
        for definition in WorkspaceTools({"repo": workspace}).definitions(): registry.register(definition)
        approval_provider = ScriptedModelProvider([ModelResponse(tool_calls=(ToolCall("write-1", "write_workspace_file", {"workspace_id": "repo", "path": "result.txt", "content": "approved"}),), finish_reason="tool_calls"), ModelResponse(completion=CompletionEnvelope(summary="approved"))])
        grant = ToolGrant("write_workspace_file", (ToolEffect.WRITE,), ("workspace:repo:*",), max_calls=1, requires_approval=True)
        approval, approval_events = await execute("approval", approval_provider, registry, _request("parity-approval", grants=(grant,), filesystem_policy="WORKSPACE_WRITE"), _AllowOnce())
        scenarios["approval"] = {"passed": approval.status == RunStatus.SUCCEEDED and (workspace / "result.txt").read_text() == "approved", "model_calls": approval_provider.call_count, "approval_events": sum(event.type == EventType.APPROVAL_REQUIRED for event in approval_events)}

    # A denial is a normal, durable outcome rather than an internal failure.
    # The worker may provide a final explanation after receiving the
    # first-party tool result, but the requested workspace mutation must not
    # occur and exactly one approval event must remain in the run record.
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        registry = ToolRegistry()
        for definition in WorkspaceTools({"repo": workspace}).definitions():
            registry.register(definition)
        denied_provider = ScriptedModelProvider(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "write-denied-1",
                            "write_workspace_file",
                            {
                                "workspace_id": "repo",
                                "path": "result.txt",
                                "content": "must not be written",
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(completion=CompletionEnvelope(summary="approval denied")),
            ]
        )
        grant = ToolGrant(
            "write_workspace_file",
            (ToolEffect.WRITE,),
            ("workspace:repo:*",),
            max_calls=1,
            requires_approval=True,
        )
        denied, denied_events = await execute(
            "approval-denied",
            denied_provider,
            registry,
            _request(
                "parity-approval-denied",
                grants=(grant,),
                filesystem_policy="WORKSPACE_WRITE",
            ),
            _Deny(),
        )
        scenarios["approval_denied"] = {
            "passed": (
                denied.status == RunStatus.SUCCEEDED
                and not (workspace / "result.txt").exists()
                and denied_provider.call_count == 2
                and sum(
                    event.type == EventType.APPROVAL_REQUIRED
                    for event in denied_events
                )
                == 1
            ),
            "model_calls": denied_provider.call_count,
            "approval_events": sum(
                event.type == EventType.APPROVAL_REQUIRED
                for event in denied_events
            ),
            "workspace_unchanged": not (workspace / "result.txt").exists(),
        }

    cancel_provider = ScriptedModelProvider([ModelResponse(completion=CompletionEnvelope(summary="never"))], blocked_calls=(0,))
    store = RunStore(); cancel_service = NoructEmployeeRuntimeService(store=store, provider=cancel_provider, registry=ToolRegistry(), python_executable=python_executable)
    try:
        handle = await cancel_service.start(_request("parity-cancel")); await cancel_provider.wait_until_started(0, timeout=3)
        await cancel_service.cancel(handle, "parity cancellation"); cancelled = await cancel_service.collect(handle)
        scenarios["cancel"] = {"passed": cancelled.status == RunStatus.CANCELLED, "model_calls": cancel_provider.call_count, "cancel_events": sum(event.type == EventType.CANCEL_REQUESTED for event in store.list_events(handle.run_id))}
    finally:
        await cancel_service.close(); store.close()

    return {
        "schema_version": "noruct.employee-runtime-preview-parity.v1",
        "provider": "deterministic_parent_contract",
        "external_model_calls": 0,
        "worker_python": python_executable,
        "scenarios": scenarios,
        "passed": all(item["passed"] for item in scenarios.values()),
    }


async def run_deferred_tool_discovery_parity(
    *, python_executable: str
) -> dict[str, Any]:
    """Prove the vendored deferred catalog stays usable through Noruct tools.

    The worker may use its private catalog machinery to search and describe a
    large tool surface.  It must not receive a second effect executor: the
    final invocation has to return to the parent registry, grant, audit log,
    and result path.  This is a deterministic exercise of that full-core seam.
    """

    store = RunStore()
    registry = ToolRegistry()
    calls: list[str] = []

    def capability(name: str) -> ToolDefinition:
        def validate(arguments: dict[str, Any]) -> dict[str, str]:
            if set(arguments) != {"key"} or not isinstance(arguments["key"], str):
                raise ValueError("key is required")
            return {"key": arguments["key"]}

        async def handle(
            arguments: dict[str, str], cancellation: CancellationToken
        ) -> str:
            cancellation.raise_if_cancelled()
            calls.append(name)
            return f"{name}:{arguments['key']}"

        return ToolDefinition(
            name=name,
            description=(
                "Find structured repository evidence for a specialized capability. "
                "This detailed description exercises deferred schema discovery. "
            ) * 3,
            input_schema={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=validate,
            resource_key=lambda arguments: f"fixture:{arguments['key']}",
            handler=handle,
        )

    names = ("target_capability", *[f"capability_{index}" for index in range(20)])
    for name in names:
        registry.register(capability(name))
    provider = ScriptedModelProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("discover", "tool_search", {"query": "target"}),),
                finish_reason="tool_calls",
            ),
            ModelResponse(
                tool_calls=(
                    ToolCall("describe", "tool_describe", {"name": "target_capability"}),
                ),
                finish_reason="tool_calls",
            ),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "invoke",
                        "tool_call",
                        {
                            "name": "target_capability",
                            "arguments": {"key": "answer"},
                        },
                    ),
                ),
                finish_reason="tool_calls",
            ),
            ModelResponse(completion=CompletionEnvelope(summary="Deferred capability completed.")),
        ]
    )
    request = replace(
        _request("parity-deferred-discovery"),
        limits=RunLimits(max_model_calls=5, max_tool_calls=2, max_wall_time_ms=15_000),
        action_policy=ActionPolicy(
            tool_grants=tuple(
                ToolGrant(name, (ToolEffect.READ,), ("fixture:*",), max_calls=2)
                for name in names
            )
        ),
    )
    service = NoructEmployeeRuntimeService(
        store=store,
        provider=provider,
        registry=registry,
        python_executable=python_executable,
    )
    try:
        result = await service.collect(await service.start(request))
        first_surface = {schema.name for schema in provider.requests[0].tools}
        action_names = [item["tool_name"] for item in store.list_tool_actions(result.run_id)]
        passed = (
            result.status == RunStatus.SUCCEEDED
            and calls == ["target_capability"]
            and action_names == ["target_capability"]
            and {"tool_search", "tool_describe", "tool_call"} <= first_surface
            and "target_capability" not in first_surface
            and result.usage.tool_calls == 1
        )
        return {
            "schema_version": "noruct.deferred-tool-discovery-parity.v1",
            "external_model_calls": 0,
            "passed": passed,
            "first_surface": sorted(first_surface),
            "parent_tool_actions": action_names,
            "resolved_calls": calls,
            "model_calls": provider.call_count,
        }
    finally:
        await service.close()
        store.close()


async def run_runtime_reliability_contract_matrix(
    *,
    python_executable: str,
    product_preview_parity: Callable[..., Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Run the deterministic reliability closure used by Noruct operators.

    This is intentionally an offline qualification, not provider, browser,
    remote-worker, gateway, or commercial-release evidence.  It combines the
    actual private worker with the public direct/Company routes so a future
    change cannot pass a narrow loop smoke test while breaking the product
    shell, approval lifecycle, Firm Kernel handoff, or answer writer.
    """

    preview, deferred_discovery, product, reroute, parallelism = await asyncio.gather(
        run_preview_parity(python_executable=python_executable),
        run_deferred_tool_discovery_parity(python_executable=python_executable),
        product_preview_parity(python_executable=python_executable),
        run_foundation_reroute_parity(python_executable=python_executable),
        run_foundation_parallelism_parity(python_executable=python_executable),
    )
    checks = {
        "direct_tool_approval_cancel": bool(preview["passed"]),
        "approval_denial_leaves_workspace_unchanged": bool(
            preview["scenarios"]["approval_denied"]["passed"]
        ),
        "deferred_tool_discovery_stays_parent_authorized": bool(
            deferred_discovery["passed"]
        ),
        "direct_company_tui_and_session": bool(product["passed"]),
        "frozen_roster_reroute": bool(reroute["passed"]),
        "dependency_ready_parallel_join": bool(parallelism["passed"]),
    }
    return {
        "schema_version": "noruct.employee-runtime-reliability.v1",
        "execution": "deterministic_offline_runtime_qualification",
        "external_model_calls": 0,
        "worker_python": python_executable,
        "passed": all(checks.values()),
        "checks": checks,
        "preview_parity": preview,
        "deferred_tool_discovery_parity": deferred_discovery,
        "product_parity": product,
        "reroute_parity": reroute,
        "parallelism_parity": parallelism,
        "scope": {
            "assessed": (
                "direct and Company route, bounded read/write/command tool loop, "
                "approval allow/deny, cancellation, resumed direct session, "
                "single terminal writer, deferred tool discovery through the parent authority, "
                "typed reroute, dependency-ready parallel join"
            ),
            "covered_by_durable_store_regression": (
                "approval compare-and-swap, exact action replay after restart, "
                "interrupted Company approval containment, employee session restart"
            ),
            "not_assessed": (
                "live provider/authentication, browser or computer sidecar, "
                "gateway ingress, scheduler daemon, remote SSH/container, hosted evolution"
            ),
        },
    }


async def run_foundation_reroute_parity(*, python_executable: str) -> dict[str, Any]:
    """Prove that a private worker cannot consume Firm-owned staffing authority.

    The Employee Runtime may report a typed assignee mismatch, but only the
    parent Firm Kernel may record the failed attempt and choose an already
    frozen, exact-capable replacement.  This is intentionally a small,
    sequential dependency graph: it proves the authoritative REROUTE boundary
    without claiming generalized workflow compilation or automatic teamwork.
    """

    store = RunStore()
    provider = ScriptedModelProvider(
        [
            ModelResponse(
                completion=CompletionEnvelope(
                    summary="This assignment needs another eligible analyst.",
                    signals=(
                        RunSignal(
                            SignalCode.ASSIGNEE_MISMATCH,
                            "analysis",
                            ("typed:mismatch",),
                        ),
                    ),
                )
            ),
            ModelResponse(
                completion=CompletionEnvelope(
                    summary="The reassigned analysis succeeded.",
                    acceptance_evidence=("analysis:evidence",),
                )
            ),
            ModelResponse(
                completion=CompletionEnvelope(
                    summary="The final integration succeeded.",
                    acceptance_evidence=("final:evidence",),
                )
            ),
        ]
    )
    service = NoructEmployeeRuntimeService(
        store=store,
        provider=provider,
        registry=ToolRegistry(),
        python_executable=python_executable,
    )
    request = CompanyRunRequest(
        request_id="foundation-reroute-parity-request",
        job_id="foundation-reroute-parity-job",
        goal="Reroute a typed mismatch within the frozen roster.",
        plan_proposal=PlanProposal(
            proposal_id="foundation-reroute-parity-plan",
            goal="Reroute a typed mismatch within the frozen roster.",
            tasks=(
                JobTask(
                    task_id="analysis",
                    objective="Complete the analysis.",
                    depends_on=(),
                    required_capabilities=("analysis",),
                    acceptance_criteria=("Analysis evidence.",),
                ),
                JobTask(
                    task_id="final",
                    objective="Integrate the analysis.",
                    depends_on=("analysis",),
                    required_capabilities=("integration",),
                    acceptance_criteria=("Integration evidence.",),
                ),
            ),
            final_task_id="final",
        ),
        roster=(
            EmployeeRecord("analyst-a", "Analyst A", ("analysis",)),
            EmployeeRecord("analyst-b", "Analyst B", ("analysis",)),
            EmployeeRecord("integrator", "Integrator", ("integration",)),
        ),
        runtime_limits=RunLimits(max_model_calls=4, max_tool_calls=4, max_cost_usd=0.0),
        job_limits=JobLimits(max_wall_time_ms=15_000, max_concurrency=1),
    )
    try:
        result = await FirmKernel(employee_execution=service).run(request)
        runs = store.list_job_runs(request.job_id)
        run_rows = tuple(
            (str(item["task_id"]), str(item["employee_id"]), str(item["status"]))
            for item in runs
        )
        mutation_types = tuple(item.mutation_type.value for item in result.mutation_events)
        passed = (
            result.status == JobStatus.SUCCEEDED
            and provider.call_count == 3
            and mutation_types == (TaskMutationType.REROUTE.value,)
            and run_rows
            == (
                ("analysis", "analyst-a", "FAILED"),
                ("analysis", "analyst-b", "SUCCEEDED"),
                ("final", "integrator", "SUCCEEDED"),
            )
            and result.metrics.maximum_parallelism == 1
        )
        return {
            "schema_version": "noruct.employee-runtime-foundation-reroute-parity.v1",
            "provider": "deterministic_parent_contract",
            "external_model_calls": 0,
            "worker_python": python_executable,
            "scenario": "typed_assignee_mismatch_to_frozen_roster_reroute",
            "status": result.status.value,
            "model_calls": provider.call_count,
            "mutation_types": mutation_types,
            "run_rows": run_rows,
            "maximum_parallelism": result.metrics.maximum_parallelism,
            "passed": passed,
        }
    finally:
        await service.close()
        store.close()


async def run_foundation_parallelism_parity(*, python_executable: str) -> dict[str, Any]:
    """Prove dependency-derived concurrency through the real employee adapter.

    Two independent analysis tasks deliberately wait at their parent-owned
    provider calls.  Seeing both calls start before either is released proves
    that the Kernel's ready-set concurrency reaches the Foundation Runtime;
    the final task may start only after both dependencies have completed.
    """

    store = RunStore()
    provider = ScriptedModelProvider(
        [
            ModelResponse(
                completion=CompletionEnvelope(
                    summary="Independent analysis A completed.",
                    acceptance_evidence=("analysis-a:evidence",),
                )
            ),
            ModelResponse(
                completion=CompletionEnvelope(
                    summary="Independent analysis B completed.",
                    acceptance_evidence=("analysis-b:evidence",),
                )
            ),
            ModelResponse(
                completion=CompletionEnvelope(
                    summary="The dependent integration completed.",
                    acceptance_evidence=("final:evidence",),
                )
            ),
        ],
        blocked_calls=(0, 1),
    )
    service = NoructEmployeeRuntimeService(
        store=store,
        provider=provider,
        registry=ToolRegistry(),
        python_executable=python_executable,
    )
    request = CompanyRunRequest(
        request_id="foundation-parallelism-parity-request",
        job_id="foundation-parallelism-parity-job",
        goal="Join two independent analyses into one final result.",
        plan_proposal=PlanProposal(
            proposal_id="foundation-parallelism-parity-plan",
            goal="Join two independent analyses into one final result.",
            tasks=(
                JobTask(
                    task_id="analysis-a",
                    objective="Complete independent analysis A.",
                    depends_on=(),
                    required_capabilities=("analysis-a",),
                    acceptance_criteria=("Analysis A evidence.",),
                ),
                JobTask(
                    task_id="analysis-b",
                    objective="Complete independent analysis B.",
                    depends_on=(),
                    required_capabilities=("analysis-b",),
                    acceptance_criteria=("Analysis B evidence.",),
                ),
                JobTask(
                    task_id="final",
                    objective="Integrate both analyses.",
                    depends_on=("analysis-a", "analysis-b"),
                    required_capabilities=("integration",),
                    acceptance_criteria=("Integration evidence.",),
                ),
            ),
            final_task_id="final",
        ),
        roster=(
            EmployeeRecord("analyst-a", "Analyst A", ("analysis-a",)),
            EmployeeRecord("analyst-b", "Analyst B", ("analysis-b",)),
            EmployeeRecord("integrator", "Integrator", ("integration",)),
        ),
        runtime_limits=RunLimits(max_model_calls=4, max_tool_calls=4, max_cost_usd=0.0),
        job_limits=JobLimits(max_wall_time_ms=15_000, max_concurrency=2),
    )
    kernel_task = asyncio.create_task(FirmKernel(employee_execution=service).run(request))
    try:
        await provider.wait_until_started(0, timeout=5)
        await provider.wait_until_started(1, timeout=5)
        parallel_starts_before_release = provider.call_count == 2
        provider.release(0)
        provider.release(1)
        result = await kernel_task
        runs = store.list_job_runs(request.job_id)
        run_rows = tuple(
            (str(item["task_id"]), str(item["employee_id"]), str(item["status"]))
            for item in runs
        )
        # ModelRequest is deliberately a transport-level shape and does not
        # expose Company context.  The redacted durable request projection is
        # the authoritative parent-owned observable for dependency injection.
        final_row = next(item for item in runs if item["task_id"] == "final")
        final_request = json.loads(str(final_row["request_json"]))
        dependency_ids = tuple(
            sorted(
                str(item["content_id"])
                for item in final_request["context"]["task_dependencies"]
            )
        )
        passed = (
            parallel_starts_before_release
            and result.status == JobStatus.SUCCEEDED
            and provider.call_count == 3
            and result.metrics.maximum_parallelism == 2
            and dependency_ids == ("task-result:analysis-a", "task-result:analysis-b")
            and run_rows
            == (
                ("analysis-a", "analyst-a", "SUCCEEDED"),
                ("analysis-b", "analyst-b", "SUCCEEDED"),
                ("final", "integrator", "SUCCEEDED"),
            )
        )
        return {
            "schema_version": "noruct.employee-runtime-foundation-parallelism-parity.v1",
            "provider": "deterministic_parent_contract",
            "external_model_calls": 0,
            "worker_python": python_executable,
            "scenario": "two_ready_tasks_then_dependency_join",
            "status": result.status.value,
            "model_calls": provider.call_count,
            "parallel_starts_before_release": parallel_starts_before_release,
            "maximum_parallelism": result.metrics.maximum_parallelism,
            "final_dependency_ids": dependency_ids,
            "run_rows": run_rows,
            "passed": passed,
        }
    finally:
        if not kernel_task.done():
            kernel_task.cancel()
            await asyncio.gather(kernel_task, return_exceptions=True)
        await service.close()
        store.close()
