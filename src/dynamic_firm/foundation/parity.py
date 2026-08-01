"""Quota-free contract matrix for the explicit Employee Runtime preview."""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
from dataclasses import replace
from pathlib import Path
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


from .runtime_parity import (  # noqa: E402
    _AllowOnce,
    run_deferred_tool_discovery_parity,
    run_foundation_parallelism_parity,
    run_foundation_reroute_parity,
    run_preview_parity,
    run_runtime_reliability_contract_matrix,
)


async def run_product_preview_parity(*, python_executable: str) -> dict[str, Any]:
    """Exercise the shipped Firm Kernel and terminal event projection offline.

    This does not claim a whole-product release gate.  It is a bounded
    integration check that proves the explicit Employee Runtime still composes
    with the existing first-party Company run path and its one answer writer.
    """

    product = product_preview_contract()
    RunCommandConfig = product.run_command_config
    run_goal = product.run_goal
    ProductEventType = product.product_event_type
    InputRoute = product.input_route
    InlineTerminalUI = product.terminal_ui

    with tempfile.TemporaryDirectory(prefix="noruct-product-preview-parity-") as directory:
        root = Path(directory)
        provider = ScriptedModelProvider(
            [
                ModelResponse(completion=CompletionEnvelope(summary="product direct parity")),
                ModelResponse(completion=CompletionEnvelope(summary="product resumed parity")),
                ModelResponse(completion=CompletionEnvelope(summary="product company parity")),
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="Solo evidence exposed a bounded gap.",
                        acceptance_evidence=("repository boundary evidence",),
                        signals=(
                            RunSignal(
                                SignalCode.CAPABILITY_MISSING,
                                "security_review",
                                ("specialist review is required",),
                            ),
                        ),
                    )
                ),
                ModelResponse(completion=CompletionEnvelope(summary="Security evidence produced.")),
                ModelResponse(completion=CompletionEnvelope(summary="Integrated company result.")),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "product-write-1",
                            "write_workspace_file",
                            {
                                "workspace_id": "noruct-workspace",
                                "path": "parity-result.txt",
                                "content": "approved product mutation\n",
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(completion=CompletionEnvelope(summary="product write parity")),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "product-edit-1",
                            "edit_workspace_file",
                            {
                                "workspace_id": "noruct-workspace",
                                "path": "parity-result.txt",
                                "old_text": "approved product mutation\n",
                                "new_text": "approved product edit\n",
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(completion=CompletionEnvelope(summary="product edit parity")),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "product-command-1",
                            "run_workspace_command",
                            {
                                "workspace_id": "noruct-workspace",
                                "command": "pwd",
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(completion=CompletionEnvelope(summary="product command parity")),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "product-chain-read-1",
                            "read_workspace_file",
                            {
                                "workspace_id": "noruct-workspace",
                                "path": "parity-result.txt",
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "product-chain-write-1",
                            "write_workspace_file",
                            {
                                "workspace_id": "noruct-workspace",
                                "path": "parity-followup.txt",
                                "content": "chained approved evidence\n",
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(completion=CompletionEnvelope(summary="product chained tool parity")),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "product-coding-command-1",
                            "run_workspace_command",
                            {
                                "workspace_id": "noruct-workspace",
                                "command": "wc -c parity-result.txt",
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "product-coding-edit-1",
                            "edit_workspace_file",
                            {
                                "workspace_id": "noruct-workspace",
                                "path": "parity-result.txt",
                                "old_text": "approved product edit\n",
                                "new_text": "approved product edit after command\n",
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "product-coding-read-1",
                            "read_workspace_file",
                            {
                                "workspace_id": "noruct-workspace",
                                "path": "parity-result.txt",
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(completion=CompletionEnvelope(summary="product coding loop parity")),
            ]
        )
        config = RunCommandConfig(
            goal="Return the bounded product parity result.",
            workspace=root,
            state_path=root / "runtime.db",
            provider_kind="openai_api",
            base_url="https://unused.invalid/v1",
            model="scripted",
            codex_model=None,
            codex_command="codex",
            api_key_env=None,
            request_timeout_seconds=5.0,
            permission_mode="read-only",
            run_limits=RunLimits(
                max_wall_time_ms=15_000,
                max_model_calls=2,
                max_tool_calls=1,
                max_cost_usd=0.0,
            ),
            employee_runtime="noruct",
            runtime_python=python_executable,
        )
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=False,
            color=False,
            animations=False,
        )
        ui.begin_goal(config.goal, echo=False)
        events: list[object] = []

        def capture(event: object) -> None:
            events.append(event)
            ui.handle_event(event)

        result = await run_goal(
            config,
            provider,
            event_sink=capture,
            route=InputRoute.CONVERSATION,
            session_key="foundation-product-preview-parity",
        )
        ui.answer(result.summary)
        ui.begin_goal("Continue the bounded product parity conversation.", echo=False)
        resumed_result = await run_goal(
            replace(config, goal="Continue the bounded product parity conversation."),
            provider,
            event_sink=capture,
            route=InputRoute.CONVERSATION,
            session_key="foundation-product-preview-parity",
        )
        ui.answer(resumed_result.summary)
        ui.begin_goal("Execute the bounded company parity path.", echo=False)
        company_result = await run_goal(
            replace(config, goal="Execute the bounded company parity path."),
            provider,
            event_sink=capture,
            route=InputRoute.COMPANY_GOAL,
            session_key="foundation-product-company-preview-parity",
        )
        ui.answer(company_result.summary)
        ui.begin_goal(
            "Inspect code and independently review security, then integrate it.",
            echo=False,
        )
        typed_gap_result = await run_goal(
            replace(
                config,
                goal=(
                    "Inspect code and independently review security, then integrate it."
                ),
                run_limits=replace(
                    config.run_limits,
                    max_model_calls=3,
                    max_wall_time_ms=30_000,
                ),
            ),
            provider,
            event_sink=capture,
            route=InputRoute.COMPANY_GOAL,
            session_key="foundation-product-typed-admission-parity",
        )
        ui.answer(typed_gap_result.summary)
        ui.begin_goal("Create the bounded approved product parity artifact.", echo=False)
        write_result = await run_goal(
            replace(
                config,
                goal="Create the bounded approved product parity artifact.",
                permission_mode="ask",
            ),
            provider,
            approval_port=_AllowOnce(),
            event_sink=capture,
            route=InputRoute.COMPANY_GOAL,
            session_key="foundation-product-write-parity",
        )
        ui.answer(write_result.summary)
        ui.begin_goal("Edit the bounded approved product parity artifact.", echo=False)
        edit_result = await run_goal(
            replace(
                config,
                goal="Edit the bounded approved product parity artifact.",
                permission_mode="ask",
            ),
            provider,
            approval_port=_AllowOnce(),
            event_sink=capture,
            route=InputRoute.COMPANY_GOAL,
            session_key="foundation-product-edit-parity",
        )
        ui.answer(edit_result.summary)
        ui.begin_goal("Run the bounded approved product parity command.", echo=False)
        command_result = await run_goal(
            replace(
                config,
                goal="Run the bounded approved product parity command.",
                permission_mode="ask",
            ),
            provider,
            approval_port=_AllowOnce(),
            event_sink=capture,
            route=InputRoute.COMPANY_GOAL,
            session_key="foundation-product-command-parity",
        )
        ui.answer(command_result.summary)
        ui.begin_goal("Read evidence then create one approved follow-up artifact.", echo=False)
        chained_tool_result = await run_goal(
            replace(
                config,
                goal="Read evidence then create one approved follow-up artifact.",
                permission_mode="ask",
                run_limits=replace(
                    config.run_limits,
                    max_model_calls=3,
                    max_tool_calls=2,
                ),
            ),
            provider,
            approval_port=_AllowOnce(),
            event_sink=capture,
            route=InputRoute.COMPANY_GOAL,
            session_key="foundation-product-chained-tool-parity",
        )
        ui.answer(chained_tool_result.summary)
        ui.begin_goal("Run a bounded command, edit its target, and verify the result.", echo=False)
        coding_loop_result = await run_goal(
            replace(
                config,
                goal="Run a bounded command, edit its target, and verify the result.",
                permission_mode="ask",
                run_limits=replace(
                    config.run_limits,
                    max_model_calls=4,
                    max_tool_calls=3,
                ),
            ),
            provider,
            approval_port=_AllowOnce(),
            event_sink=capture,
            route=InputRoute.COMPANY_GOAL,
            session_key="foundation-product-coding-loop-parity",
        )
        ui.answer(coding_loop_result.summary)
        # Exercise the already-vendored, Paperclip-derived budget boundary
        # through the public Company route. A hard stop must happen before a
        # provider call and must never auto-resume or modify the policy.
        with CompanyStateStore(config.state_path) as company_store:
            company_store.set_company_cost_budget_policy(
                {"max_total_cost_usd": 0.10, "window_kind": "lifetime"},
                actor="parity:company-budget",
            )
        ui.begin_goal("Prove a company budget hard stop before dispatch.", echo=False)
        budget_result = await run_goal(
            replace(
                config,
                goal="Prove a company budget hard stop before dispatch.",
                run_limits=replace(config.run_limits, max_cost_usd=0.25),
            ),
            provider,
            event_sink=capture,
            route=InputRoute.COMPANY_GOAL,
            session_key="foundation-product-company-budget-parity",
        )
        ui.answer(budget_result.summary)
        event_types = {getattr(event, "type", None) for event in events}
        required_events = {
            ProductEventType.INPUT_ROUTED,
            ProductEventType.PLAN_ACCEPTED,
            ProductEventType.EMPLOYEE_STARTED,
            ProductEventType.EMPLOYEE_FINISHED,
            ProductEventType.JOB_FINISHED,
        }
        rendered = output.getvalue()
        passed = (
            result.status.value == "SUCCEEDED"
            and result.summary == "product direct parity"
            and resumed_result.status.value == "SUCCEEDED"
            and resumed_result.summary == "product resumed parity"
            and company_result.status.value == "SUCCEEDED"
            and company_result.summary == "product company parity"
            and typed_gap_result.status.value == "SUCCEEDED"
            and typed_gap_result.summary == "Integrated company result."
            and typed_gap_result.metrics.organization_admission_count == 1
            and typed_gap_result.metrics.graph_patch_count == 1
            # This fixture now names an independent reviewer explicitly.  The
            # evidence admission may therefore create the typed missing
            # specialist plus the requested reviewer; the invariant is a
            # bounded non-zero temporary role count, not a historical count.
            and typed_gap_result.metrics.temporary_role_count >= 1
            and write_result.status.value == "SUCCEEDED"
            and write_result.summary == "product write parity"
            and edit_result.status.value == "SUCCEEDED"
            and edit_result.summary == "product edit parity"
            and command_result.status.value == "SUCCEEDED"
            and command_result.summary == "product command parity"
            and chained_tool_result.status.value == "SUCCEEDED"
            and chained_tool_result.summary == "product chained tool parity"
            and coding_loop_result.status.value == "SUCCEEDED"
            and coding_loop_result.summary == "product coding loop parity"
            and budget_result.status.value == "BUDGET_EXHAUSTED"
            and "explicit operator budget resolution" in budget_result.summary.lower()
            and (root / "parity-result.txt").read_text(encoding="utf-8")
            == "approved product edit after command\n"
            and (root / "parity-followup.txt").read_text(encoding="utf-8")
            == "chained approved evidence\n"
            and provider.call_count == 19
            and any(
                "product direct parity" in str(message.content)
                for message in provider.requests[1].messages
            )
            and required_events.issubset(event_types)
            and ProductEventType.WORKSPACE_IDENTITY in event_types
            and sum(event.type == ProductEventType.JOB_FINISHED for event in events) == 10
            and rendered.count("product direct parity") == 1
            and rendered.count("product resumed parity") == 1
            and rendered.count("product company parity") == 1
            and rendered.count("Integrated company result.") == 1
            and rendered.count("product write parity") == 1
            and rendered.count("product edit parity") == 1
            and rendered.count("product command parity") == 1
            and rendered.count("product chained tool parity") == 1
            and rendered.count("product coding loop parity") == 1
            and rendered.count("● Noruct") == 10
        )
        return {
            "schema_version": "noruct.employee-runtime-product-parity.v1",
            "provider": "deterministic_parent_contract",
            "external_model_calls": 0,
            "worker_python": python_executable,
            "scenarios": {
                "direct_conversation": result.status.value,
                "resumed_direct_conversation": resumed_result.status.value,
                "single_task_company_goal": company_result.status.value,
                "typed_capability_admission": typed_gap_result.status.value,
                "approved_workspace_write": write_result.status.value,
                "approved_workspace_edit": edit_result.status.value,
                "approved_workspace_command": command_result.status.value,
                "approved_workspace_read_then_write": chained_tool_result.status.value,
                "approved_workspace_command_edit_verify": coding_loop_result.status.value,
                "company_budget_pre_dispatch_hard_stop": budget_result.status.value,
            },
            "scenario_summaries": {
                "direct_conversation": result.summary,
                "resumed_direct_conversation": resumed_result.summary,
                "single_task_company_goal": company_result.summary,
                "typed_capability_admission": typed_gap_result.summary,
                "approved_workspace_write": write_result.summary,
                "approved_workspace_edit": edit_result.summary,
                "approved_workspace_command": command_result.summary,
                "approved_workspace_read_then_write": chained_tool_result.summary,
                "approved_workspace_command_edit_verify": coding_loop_result.summary,
                "company_budget_pre_dispatch_hard_stop": budget_result.summary,
            },
            "typed_admission_metrics": {
                "organization_admission_count": typed_gap_result.metrics.organization_admission_count,
                "graph_patch_count": typed_gap_result.metrics.graph_patch_count,
                "temporary_role_count": typed_gap_result.metrics.temporary_role_count,
            },
            "model_calls": provider.call_count,
            "required_product_events": sorted(item.value for item in required_events),
            "observed_product_events": sorted(
                item.value for item in event_types if isinstance(item, ProductEventType)
            ),
            "single_answer_writer": (
                rendered.count("product direct parity") == 1
                and rendered.count("product resumed parity") == 1
                and rendered.count("product company parity") == 1
                and rendered.count("Integrated company result.") == 1
                and rendered.count("product write parity") == 1
                and rendered.count("product edit parity") == 1
                and rendered.count("product command parity") == 1
                and rendered.count("product chained tool parity") == 1
                and rendered.count("product coding loop parity") == 1
            ),
            "rendered_answer_counts": {
                "product_direct": rendered.count("product direct parity"),
                "product_resumed": rendered.count("product resumed parity"),
                "product_company": rendered.count("product company parity"),
                "integrated_company": rendered.count("Integrated company result."),
                "product_write": rendered.count("product write parity"),
                "product_edit": rendered.count("product edit parity"),
                "product_command": rendered.count("product command parity"),
                "product_chained": rendered.count("product chained tool parity"),
                "product_coding_loop": rendered.count("product coding loop parity"),
                "answer_markers": rendered.count("● Noruct"),
            },
            "passed": passed,
        }


async def run_product_mcp_preview_parity(
    *,
    python_executable: str,
    mcp_python: str,
    server_script: Path,
) -> dict[str, Any]:
    """Prove the optional user-managed MCP path through the product runtime.

    The SDK and test server remain outside Noruct's distribution.  This
    function deliberately receives their absolute paths so no product default,
    dependency, discovery, or credential behavior changes merely by running
    an offline parity test.
    """

    product = product_preview_contract()
    RunCommandConfig = product.run_command_config
    run_goal = product.run_goal
    ProductEventType = product.product_event_type
    InputRoute = product.input_route
    InlineTerminalUI = product.terminal_ui

    with tempfile.TemporaryDirectory(prefix="noruct-product-mcp-parity-") as directory:
        root = Path(directory)
        mcp_config = McpReadOnlyConfig(
            python_command=Path(mcp_python).absolute(),
            server_command=Path(mcp_python).absolute(),
            server_args=(str(server_script.absolute()), "multi_read"),
            tool_names=("read_issue", "read_note"),
            profile="product-preview",
            timeout_seconds=3.0,
            max_result_bytes=48_000,
        )
        issue_tool, note_tool = mcp_config.selected_runtime_tool_names()
        provider = ScriptedModelProvider(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "product-mcp-read-1",
                            issue_tool,
                            {"query": "product parity"},
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "product-mcp-read-2",
                            note_tool,
                            {"query": "product parity"},
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(completion=CompletionEnvelope(summary="product MCP parity")),
            ]
        )
        config = RunCommandConfig(
            goal="Read two bounded external contexts and summarize them.",
            workspace=root,
            state_path=root / "runtime.db",
            provider_kind="openai_api",
            base_url="https://unused.invalid/v1",
            model="scripted",
            codex_model=None,
            codex_command="codex",
            api_key_env=None,
            request_timeout_seconds=5.0,
            permission_mode="read-only",
            run_limits=RunLimits(
                max_wall_time_ms=20_000,
                max_model_calls=3,
                max_tool_calls=2,
                max_cost_usd=0.0,
            ),
            mcp_read_only=mcp_config,
            employee_runtime="noruct",
            runtime_python=python_executable,
        )
        output = io.StringIO()
        ui = InlineTerminalUI(
            stdin=io.StringIO(),
            stdout=output,
            interactive=False,
            color=False,
            animations=False,
        )
        events: list[object] = []

        def capture(event: object) -> None:
            events.append(event)
            ui.handle_event(event)

        ui.begin_goal(config.goal, echo=False)
        result = await run_goal(
            config,
            provider,
            event_sink=capture,
            route=InputRoute.COMPANY_GOAL,
            session_key="foundation-product-mcp-preview-parity",
        )
        ui.answer(result.summary)
        event_types = {getattr(event, "type", None) for event in events}
        rendered = output.getvalue()
        ledger = RunStore(config.state_path)
        try:
            ledger_resources = tuple(
                str(action["resource_key"])
                for run in ledger.list_job_runs(result.job_id)
                for action in ledger.list_tool_actions(str(run["run_id"]))
            )
        finally:
            ledger.close()
        passed = (
            result.status == RunStatus.SUCCEEDED
            and result.summary == "product MCP parity"
            and result.metrics.usage.tool_calls == 2
            and provider.call_count == 3
            and ProductEventType.CAPABILITY_READY in event_types
            and ProductEventType.TOOL_REQUESTED in event_types
            and ProductEventType.TOOL_FINISHED in event_types
            and ProductEventType.JOB_FINISHED in event_types
            and "read_issue" not in str(events)
            and "read_note" not in str(events)
            and len(ledger_resources) == 2
            and "read_issue" not in " ".join(ledger_resources)
            and "read_note" not in " ".join(ledger_resources)
            and rendered.count("product MCP parity") == 1
            and rendered.count("● Noruct") == 1
        )
        return {
            "schema_version": "noruct.employee-runtime-product-mcp-parity.v1",
            "provider": "deterministic_parent_contract",
            "external_model_calls": 0,
            "worker_python": python_executable,
            "mcp_sdk_python": mcp_python,
            "scenarios": {"user_managed_multi_external_read": result.status.value},
            "observed_product_events": sorted(
                item.value for item in event_types if isinstance(item, ProductEventType)
            ),
            "single_answer_writer": rendered.count("product MCP parity") == 1,
            "passed": passed,
        }


async def run_runtime_reliability_qualification(
    *, python_executable: str
) -> dict[str, Any]:
    """Compose runtime and product parity through the public facade."""

    return await run_runtime_reliability_contract_matrix(
        python_executable=python_executable,
        product_preview_parity=run_product_preview_parity,
    )
