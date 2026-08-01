from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.coding import CodingWorkResult
from dynamic_firm.company import (
    CompanyLearningService,
    CompanyStateStore,
    EmployeeSkillPatchService,
    EmployeeSkillPatchStatus,
    EmployeeSkillProcedure,
    EvidenceSource,
    HiringRecommendationService,
    MANAGER_CAPABILITY,
    OrganizationEpisode,
    RosterPatchService,
    RosterPatchStatus,
    StaffingDemandEvidence,
    WorkflowPatchStatus,
    WorkspaceProjectionError,
    WorkspaceProjectionFailureCode,
    decode_active_roster,
    project_workspace_structure,
    WorkflowTaskTemplate,
    workflow_context_fingerprint_v2,
)
from dynamic_firm.cli import (
    EXIT_INPUT,
    EXIT_JOB_FAILED,
    EXIT_OK,
    _activate_interactive_session,
    _resolve_interactive_terminal_ui,
    build_parser,
    main,
)
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.providers.anthropic import AnthropicProviderConfig
from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.product.sessions import CompanySessionStore
from dynamic_firm.product.tui import CLEAR_SCREEN
from dynamic_firm.runtime.models import (
    CompletionEnvelope,
    ModelResponse,
    RunSignal,
    SignalCode,
    StructuredOutputResponse,
    ToolCall,
    Usage,
)
from dynamic_firm.runtime.job_ledger import ActiveJobInspector
from dynamic_firm.runtime.store import RunStore
from tests.providers.test_openai_compat import completion_body, contract_server
from tests.company.test_company_kernel import episode as company_episode


PROJECT_ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "tiny_repo"


def fixture_workflow_context() -> str:
    return workflow_context_fingerprint_v2(
        project_workspace_structure(FIXTURE_ROOT, "READ_ONLY")
    )


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class SoloStructuredProvider:
    async def complete_structured(self, request, cancellation):
        cancellation.raise_if_cancelled()
        return StructuredOutputResponse(
            compiler_plan(
                "SOLO",
                [compiler_task("implement_change")],
                "implement_change",
            ),
            usage=Usage(input_tokens=5, output_tokens=3),
        )


class PriorCapturingProvider(ScriptedModelProvider):
    def __init__(self) -> None:
        super().__init__(
            [ModelResponse(completion=CompletionEnvelope(summary="Prior-aware result."))]
        )
        self.structured_requests = []

    async def complete_structured(self, request, cancellation):
        cancellation.raise_if_cancelled()
        self.structured_requests.append(request)
        return StructuredOutputResponse(
            compiler_plan("SOLO", [compiler_task("analyze")], "analyze"),
            usage=Usage(model_calls=1),
        )


class TeamStructuredProvider(ScriptedModelProvider):
    """Exercise the real Company compiler and concurrent employee path."""

    def __init__(self) -> None:
        super().__init__(
            [
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="General market evidence prepared.",
                        acceptance_evidence=("general-evidence",),
                    )
                ),
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="Repository evidence prepared.",
                        acceptance_evidence=("repository-evidence",),
                    )
                ),
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="The company integrated both independent findings.",
                        acceptance_evidence=("integrated-evidence",),
                    )
                ),
            ]
        )
        self.structured_requests = []

    async def complete_structured(self, request, cancellation):
        cancellation.raise_if_cancelled()
        self.structured_requests.append(request)
        return StructuredOutputResponse(
            compiler_plan(
                "GRAPH",
                [
                    compiler_task(
                        "analyze_market",
                        capability="general_reasoning",
                    ),
                    compiler_task(
                        "inspect_repository",
                        capability="repository_analysis",
                    ),
                    compiler_task(
                        "integrate_findings",
                        depends_on=("analyze_market", "inspect_repository"),
                        capability="evidence_synthesis",
                    ),
                ],
                "integrate_findings",
            ),
            usage=Usage(input_tokens=7, output_tokens=5),
            provider_request_id="company-plan-1",
        )


class RosterCapturingProvider(ScriptedModelProvider):
    def __init__(
        self,
        summary: str = "Persistent employee completed the task.",
        capability: str = "persistent_capability",
    ) -> None:
        super().__init__(
            [ModelResponse(completion=CompletionEnvelope(summary=summary))]
        )
        self.capability = capability
        self.structured_requests = []

    async def complete_structured(self, request, cancellation):
        cancellation.raise_if_cancelled()
        self.structured_requests.append(request)
        return StructuredOutputResponse(
            compiler_plan(
                "SOLO",
                [compiler_task("persistent_task", capability=self.capability)],
                "persistent_task",
            )
        )


class CliShadowWorker:
    def __init__(self) -> None:
        self.workspaces = []

    async def execute(self, request, cancellation):
        cancellation.raise_if_cancelled()
        self.workspaces.append(request.workspace)
        (request.workspace / "created.py").write_text("value = 2\n", encoding="utf-8")
        return CodingWorkResult(summary="Prepared and applied a validated shadow change.")


def compiler_body(value: dict, *, request_id: str = "chatcmpl-plan") -> dict:
    return {
        "id": request_id,
        "choices": [
            {
                "message": {"role": "assistant", "content": json.dumps(value)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 13, "completion_tokens": 9},
    }


def compiler_task(
    task_id: str,
    *,
    depends_on=(),
    capability: str = "repository_analysis",
) -> dict:
    return {
        "task_id": task_id,
        "objective": f"Complete {task_id}",
        "depends_on": list(depends_on),
        "required_capabilities": [capability],
        "acceptance_criteria": [f"Evidence for {task_id}"],
        "risk_level": "LOW",
    }


def compiler_plan(mode: str, tasks: list[dict], final_task_id: str) -> dict:
    return {
        "mode": mode,
        "rationale": "This is the smallest useful execution plan.",
        "assumptions": [],
        "tasks": tasks,
        "final_task_id": final_task_id,
    }


class CliTests(unittest.TestCase):
    def test_portfolio_submit_preview_and_drain_use_the_same_front_door(self) -> None:
        """A queued Work Order must reach Kernel without a second request authority."""

        provider = PriorCapturingProvider()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "runtime.db"
            common = [
                "--workspace", str(FIXTURE_ROOT), "--state", str(state),
                "--base-url", "http://127.0.0.1:9/v1", "--model", "portfolio-contract",
                "--no-auth", "--permission-mode", "read-only",
            ]
            submitted_output = io.StringIO()
            submitted = main(
                [
                    "portfolio", "submit", "Inspect the repository and return evidence",
                    *common, "--confirm", "--json",
                ],
                provider_factory=lambda _config: self.fail("portfolio submit must not build a provider"),
                stdout=submitted_output,
                stderr=io.StringIO(),
            )
            submitted_payload = json.loads(submitted_output.getvalue())
            preview_output = io.StringIO()
            preview = main(
                ["portfolio", "preview", "--state", str(state), "--json"],
                stdout=preview_output,
                stderr=io.StringIO(),
            )
            preview_payload = json.loads(preview_output.getvalue())
            drained_output = io.StringIO()
            drained = main(
                ["portfolio", "drain", *common, "--confirm", "--json"],
                provider_factory=lambda _config: provider,
                stdout=drained_output,
                stderr=io.StringIO(),
            )
            drained_payload = json.loads(drained_output.getvalue())
            status_output = io.StringIO()
            status = main(
                ["portfolio", "status", "--state", str(state), "--json"],
                stdout=status_output,
                stderr=io.StringIO(),
            )
            status_payload = json.loads(status_output.getvalue())

        self.assertEqual(submitted, EXIT_OK)
        self.assertEqual(preview, EXIT_OK)
        self.assertEqual(drained, EXIT_OK)
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(submitted_payload["execution"], "NOT_STARTED")
        self.assertEqual(preview_payload["execution"], "NOT_STARTED")
        self.assertEqual(len(drained_payload["result"]["settled_job_ids"]), 1)
        self.assertEqual(status_payload["entries"][0]["status"], "CLOSED")
        self.assertEqual(
            status_payload["entries"][0]["work_order_id"],
            submitted_payload["entry"]["work_order_id"],
        )

    def test_portfolio_drain_returns_live_company_budget_denial_to_deferred(self) -> None:
        provider = PriorCapturingProvider()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "runtime.db"
            common = [
                "--workspace", str(FIXTURE_ROOT), "--state", str(state),
                "--base-url", "http://127.0.0.1:9/v1", "--model", "portfolio-budget",
                "--no-auth", "--permission-mode", "read-only", "--max-cost-usd", "2.0",
            ]
            with CompanyStateStore(state) as company:
                company.set_company_cost_budget_policy(
                    {"max_total_cost_usd": 1.0, "window_kind": "lifetime"},
                    actor="portfolio-e2e-test",
                )
            self.assertEqual(
                main(
                    ["portfolio", "submit", "Inspect the repository", *common, "--confirm"],
                    provider_factory=lambda _config: self.fail("submission must not build a provider"),
                    stdout=io.StringIO(), stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            output = io.StringIO()
            self.assertEqual(
                main(
                    ["portfolio", "drain", *common, "--confirm", "--json"],
                    provider_factory=lambda _config: provider,
                    stdout=output, stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            payload = json.loads(output.getvalue())
            status_output = io.StringIO()
            main(
                ["portfolio", "status", "--state", str(state), "--json"],
                stdout=status_output, stderr=io.StringIO(),
            )
            status = json.loads(status_output.getvalue())

        self.assertEqual(len(payload["result"]["deferred_work_order_ids"]), 1)
        self.assertEqual(payload["result"]["settled_job_ids"], [])
        self.assertEqual(status["entries"][0]["status"], "DEFERRED")
        self.assertIsNone(status["entries"][0]["job_id"])
        self.assertEqual(status["settlements"], [])

    def test_no_arguments_prints_company_help(self) -> None:
        output = io.StringIO()
        error = io.StringIO()

        exit_code = main([], stdout=output, stderr=error)

        self.assertEqual(exit_code, EXIT_OK)
        self.assertIn("Give one goal", output.getvalue())
        self.assertIn("doctor", output.getvalue())
        self.assertEqual(error.getvalue(), "")

    def test_continue_read_only_is_a_named_command_not_a_bare_goal(self) -> None:
        """The recovery command must survive argv normalization before dispatch."""

        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            error = io.StringIO()
            exit_code = main(
                [
                    "continue-read-only",
                    "missing-job",
                    "--state",
                    str(Path(temporary) / "runtime.db"),
                    "--confirm",
                ],
                stdout=output,
                stderr=error,
            )

        self.assertEqual(exit_code, EXIT_INPUT)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("No retained read-only continuation request", error.getvalue())

    def test_handoff_read_only_is_a_named_command_not_a_bare_goal(self) -> None:
        """Authority handoff is explicit and never falls through to normal run."""

        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            error = io.StringIO()
            exit_code = main(
                [
                    "handoff-read-only",
                    "missing-job",
                    "device-laptop-b",
                    "--state",
                    str(Path(temporary) / "runtime.db"),
                    "--confirm",
                ],
                stdout=output,
                stderr=error,
            )

        self.assertEqual(exit_code, EXIT_INPUT)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("No retained read-only continuation request", error.getvalue())

    def test_graph_proposal_decision_requires_confirmation_before_state_lookup(self) -> None:
        """A durable topology decision must never read or resolve state by accident."""

        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            error = io.StringIO()
            exit_code = main(
                [
                    "continue-graph-proposal",
                    "missing-job",
                    "graph-proposal-missing",
                    "approve",
                    "--state",
                    str(Path(temporary) / "runtime.db"),
                ],
                stdout=output,
                stderr=error,
            )

        self.assertEqual(exit_code, EXIT_INPUT)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Graph proposal decision requires --confirm", error.getvalue())

    def test_graph_commands_persist_a_surface_neutral_selection(self) -> None:
        payload = {
            "blueprint_id": "local-analysis",
            "version": 1,
            "objective_class": "general",
            "execution_profiles": ["read_only"],
            "parameters": ["objective"],
            "tasks": [{
                "task_id": "final",
                "objective_template": "Analyze {{objective}}",
                "depends_on": [],
                "required_capabilities": ["analysis"],
                "acceptance_templates": ["A concise result"],
                "risk_level": "LOW",
            }],
            "final_task_id": "final",
            "origin": "DRAFT",
            "parent_ref": None,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "runtime.db"
            source = root / "blueprint.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            output = io.StringIO()
            self.assertEqual(
                main(
                    ["graph", "import", str(source), "--state", str(state), "--confirm", "--json"],
                    stdout=output,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            imported = json.loads(output.getvalue())
            with CompanyStateStore(state) as company_store:
                company_store.ensure_roster_baseline(
                    (
                        EmployeeRecord(
                            employee_id="analysis-employee",
                            role="Analysis Employee",
                            capabilities=("analysis",),
                            model_profile="codex-default",
                        ),
                    )
                )
            output = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "graph", "select", "local-analysis", "1", "--mutation-policy", "LOCKED",
                        "--state", str(state), "--confirm", "--json",
                    ],
                    stdout=output,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            selected = json.loads(output.getvalue())
            self.assertEqual(selected["selection"]["blueprint_ref"]["blueprint_id"], "local-analysis")
            self.assertEqual(selected["selection"]["constraints"]["mutation_policy"], "LOCKED")
            output = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "graph", "preview", "Analyze the release notes",
                        "--state", str(state), "--workspace", str(FIXTURE_ROOT),
                        "--provider", "openai-codex", "--codex-command", "echo", "--json",
                    ],
                    stdout=output,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            preview = json.loads(output.getvalue())
            self.assertEqual(preview["work_mode"], "SOLO_JOB")
            self.assertEqual(preview["admission_status"], "ADMITTED")
            self.assertEqual(preview["tasks"][0]["proposed_employee_id"], "analysis-employee")
            output = io.StringIO()
            self.assertEqual(
                main(["graph", "list", "--state", str(state), "--json"], stdout=output, stderr=io.StringIO()),
                EXIT_OK,
            )
            listed = json.loads(output.getvalue())
            self.assertEqual(listed["schema"], "noruct.graph-control.v1")
            self.assertEqual(len(listed["blueprints"]), 1)
            revision_payload = {
                **payload,
                "version": 2,
                "origin": "USER_REVISION",
                "parent_ref": {
                    "blueprint_id": "local-analysis",
                    "version": 1,
                    "content_digest": imported["content_digest"],
                },
                "tasks": [{
                    **payload["tasks"][0],
                    "objective_template": "Analyze {{objective}} with explicit evidence",
                }],
            }
            revision_file = root / "revision.json"
            revision_file.write_text(json.dumps(revision_payload), encoding="utf-8")
            output = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "graph", "revise", "local-analysis", "1", str(revision_file),
                        "--reason", "Clarify the requested analysis evidence.",
                        "--state", str(state), "--confirm", "--json",
                    ],
                    stdout=output,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            revised = json.loads(output.getvalue())
            self.assertEqual(revised["blueprint"]["version"], 2)
            self.assertEqual(revised["revision_receipt"]["status"], "ACCEPTED")
            output = io.StringIO()
            self.assertEqual(
                main(
                    ["graph", "history", "local-analysis", "--state", str(state), "--json"],
                    stdout=output,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            self.assertEqual(len(json.loads(output.getvalue())["revision_receipts"]), 1)

    def test_natural_graph_edit_emits_but_does_not_save_a_candidate(self) -> None:
        payload = {
            "blueprint_id": "natural-analysis",
            "version": 1,
            "objective_class": "general",
            "execution_profiles": ["read_only"],
            "parameters": ["objective", "requested_outcome"],
            "tasks": [{
                "task_id": "final",
                "objective_template": "Analyze {{objective}}",
                "depends_on": [],
                "required_capabilities": ["analysis"],
                "acceptance_templates": ["Answer {{requested_outcome}}"],
                "risk_level": "LOW",
            }],
            "final_task_id": "final",
            "origin": "DRAFT",
            "parent_ref": None,
        }

        class NaturalEditProvider:
            async def complete_structured(self, request, cancellation):  # type: ignore[no-untyped-def]
                cancellation.raise_if_cancelled()
                self.request = request
                return StructuredOutputResponse(
                    {
                        "rationale": "Collect evidence before integration.",
                        "objective_class": "general",
                        "execution_profiles": ["read_only"],
                        "parameters": ["objective", "requested_outcome"],
                        "tasks": [
                            {
                                "task_id": "evidence",
                                "objective_template": "Collect {{objective}} evidence",
                                "depends_on": [],
                                "required_capabilities": ["analysis"],
                                "acceptance_templates": ["Evidence"],
                                "risk_level": "LOW",
                                "execution_replica": None,
                            },
                            {
                                "task_id": "final",
                                "objective_template": "Analyze {{objective}}",
                                "depends_on": ["evidence"],
                                "required_capabilities": ["analysis"],
                                "acceptance_templates": ["Answer {{requested_outcome}}"],
                                "risk_level": "LOW",
                                "execution_replica": None,
                            },
                        ],
                        "final_task_id": "final",
                    },
                    usage=Usage(model_calls=1),
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "runtime.db"
            source = root / "blueprint.json"
            candidate_file = root / "candidate.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(
                main(
                    ["graph", "import", str(source), "--state", str(state), "--confirm"],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            provider = NaturalEditProvider()
            output = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "graph", "natural-edit", "natural-analysis", "1",
                        "Add evidence first.", "--state", str(state),
                        "--workspace", str(FIXTURE_ROOT), "--provider", "openai-codex",
                        "--codex-command", "echo", "--output", str(candidate_file),
                        "--confirm", "--json",
                    ],
                    provider_factory=lambda _config: provider,
                    stdout=output,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            rendered = json.loads(output.getvalue())
            self.assertEqual(rendered["natural_graph_edit"]["runtime_effect"], "NONE_REQUIRES_EXPLICIT_GRAPH_REVISE_CONFIRM")
            self.assertTrue(candidate_file.is_file())
            self.assertEqual(json.loads(candidate_file.read_text(encoding="utf-8"))["version"], 2)
            listed = io.StringIO()
            self.assertEqual(
                main(["graph", "list", "--state", str(state), "--json"], stdout=listed, stderr=io.StringIO()),
                EXIT_OK,
            )
            self.assertEqual(len(json.loads(listed.getvalue())["blueprints"]), 1)

    def test_modern_terminal_without_optional_profile_is_actionable(self) -> None:
        output = TtyStringIO()
        error = io.StringIO()
        with patch(
            "dynamic_firm.application.interactive_runtime_cli.cli.modern_terminal_available",
            return_value=False,
        ):
            with tempfile.TemporaryDirectory() as temporary:
                exit_code = main(
                    [
                        "chat",
                        "--terminal-ui",
                        "modern",
                        "--workspace",
                        str(FIXTURE_ROOT),
                        "--state",
                        str(Path(temporary) / "runtime.db"),
                    ],
                    stdin=TtyStringIO(),
                    stdout=output,
                    stderr=error,
                )

        self.assertEqual(exit_code, EXIT_INPUT)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("pip install 'noruct[modern-tui]'", error.getvalue())

    def test_auto_terminal_selects_modern_only_when_the_audited_profile_is_available(self) -> None:
        parser = build_parser()
        auto = parser.parse_args(["chat"])
        native = parser.parse_args(["chat", "--terminal-ui", "native"])
        plain = parser.parse_args(["chat", "--plain"])
        scrollback_safe = parser.parse_args(["chat", "--no-live-screen"])

        with patch(
            "dynamic_firm.application.interactive_runtime_cli.cli.modern_terminal_available",
            return_value=True,
        ):
            self.assertEqual(auto.terminal_ui, "auto")
            self.assertEqual(_resolve_interactive_terminal_ui(auto), "modern")
            self.assertEqual(
                _resolve_interactive_terminal_ui(
                    auto,
                    stdin=TtyStringIO(),
                    stdout=TtyStringIO(),
                ),
                "native",
            )
            self.assertEqual(_resolve_interactive_terminal_ui(native), "native")
            self.assertEqual(_resolve_interactive_terminal_ui(plain), "native")
            self.assertEqual(_resolve_interactive_terminal_ui(scrollback_safe), "native")

        with patch(
            "dynamic_firm.application.interactive_runtime_cli.cli.modern_terminal_available",
            return_value=False,
        ):
            self.assertEqual(_resolve_interactive_terminal_ui(auto), "native")

    def test_modern_controller_knowledge_commands_do_not_build_a_provider_or_add_turns(self) -> None:
        import asyncio

        from dynamic_firm.cli import _ModernInteractiveController, _load_config
        from dynamic_firm.knowledge.store import KnowledgeStore, knowledge_state_path

        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            skill_root = Path(temporary) / "skills"
            skill_root.mkdir()
            (skill_root / "SKILL.md").write_text("# Local review skill\n", encoding="utf-8")
            receipt_path = Path(temporary) / "remote-receipt.json"
            digest = "a" * 64
            receipt_path.write_text(
                json.dumps(
                    {
                        "host": "worker.example.test",
                        "user": "operator",
                        "port": 22,
                        "remote_snapshot_directory": f"/tmp/.noruct-remote-snapshots/{digest}",
                        "snapshot_sha256": digest,
                        "transferred": True,
                        "integrity_state": "VERIFIED_REMOTE_SNAPSHOT",
                        "host_key_policy": "STRICT_KNOWN_HOSTS_ONLY",
                        "remote_job_execution": "NOT_IMPLEMENTED",
                    }
                ),
                encoding="utf-8",
            )
            args = build_parser().parse_args(
                [
                    "--config",
                    str(Path(temporary) / "config.toml"),
                    "chat",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(state_path),
                    "--provider",
                    "openai-api",
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "contract-model",
                    "--no-auth",
                    "--permission-mode",
                    "read-only",
                ]
            )
            controller = _ModernInteractiveController(
                args,
                {},
                provider_factory=lambda config: self.fail(
                    "modern local Knowledge commands must not build a provider"
                ),
                coding_worker_factory=lambda config: self.fail(
                    "modern local Knowledge commands must not build a coding worker"
                ),
            )
            try:
                remembered = asyncio.run(
                    controller.execute_command("/remember Modern local Cedar note")
                )
                retrieved = asyncio.run(controller.execute_command("/knowledge Cedar"))
                help_result = asyncio.run(controller.execute_command("/help"))
                slash_result = asyncio.run(controller.execute_command("/"))
                tools_before = asyncio.run(controller.execute_command("/tools"))
                permission_result = asyncio.run(
                    controller.execute_command("/permission ask")
                )
                limit_result = asyncio.run(
                    controller.execute_command("/setting run.max_tool_calls 21")
                )
                connection_result = asyncio.run(
                    controller.execute_command(
                        '/connection {"provider_kind":"openrouter","model":"openrouter/cheap-contract","request_timeout":45}'
                    )
                )
                custom_endpoint_result = asyncio.run(
                    controller.execute_command(
                        '/connection {"base_url":"https://gateway.example.test/v1","model":"openrouter/custom-contract"}'
                    )
                )
                model_only_result = asyncio.run(
                    controller.execute_command('/connection {"model":"openrouter/final-contract"}')
                )
                tools_after = asyncio.run(controller.execute_command("/tools"))
                settings_result = asyncio.run(controller.execute_command("/settings"))
                graph_result = asyncio.run(controller.execute_command("/graph"))
                job_result = asyncio.run(controller.execute_command("/job"))
                job_snapshot = controller.job_audit_snapshot()
                graph_before = controller.graph_control_snapshot()
                graph_saved = controller.apply_graph_control(
                    {
                        "blueprint_id": None,
                        "version": None,
                        "pinned_employee_ids": ("employee-company-generalist",),
                        "excluded_employee_ids": (),
                        "require_independent_review": True,
                        "max_concurrency": 1,
                        "max_cost_usd": 0.5,
                        "max_wall_time_ms": 10_000,
                        "mutation_policy": "PROPOSE",
                    }
                )
                graph_after = controller.graph_control_snapshot()
                environment_settings_result = asyncio.run(controller.execute_command("/settings Environment"))
                capabilities_result = asyncio.run(controller.execute_command("/capabilities"))
                search_result = asyncio.run(
                    controller.execute_command("/quick-web-search http://127.0.0.1:8080")
                )
                browser_result = asyncio.run(
                    controller.execute_command(
                        "/quick-browser "
                        + json.dumps(
                            {
                                "node_command": "/usr/bin/env",
                                "cdp_endpoint": "http://127.0.0.1:9222",
                                "allow_control": True,
                                "capture_directory": str(FIXTURE_ROOT),
                            }
                        )
                    )
                )
                computer_result = asyncio.run(
                    controller.execute_command(
                        '/quick-computer {"driver_command":"/usr/bin/env","allowed_apps":["Finder"],"allow_control":true}'
                    )
                )
                media_result = asyncio.run(
                    controller.execute_command('/quick-media {"api_key_env":"MEDIA_TEST_KEY","capabilities":"image,speech"}')
                )
                mcp_result = asyncio.run(
                    controller.execute_command(
                        '/quick-mcp {"python_command":"/usr/bin/env","server_command":"/usr/bin/env","tool_name":"read_context"}'
                    )
                )
                mcp_action_result = asyncio.run(
                    controller.execute_command(
                        '/quick-mcp-action {"python_command":"/usr/bin/env","server_command":"/usr/bin/env","tool_name":"run_action"}'
                    )
                )
                home_assistant_result = asyncio.run(
                    controller.execute_command(
                        '/quick-home-assistant {"base_url":"http://127.0.0.1:8123","token_env":"HASS_TEST_TOKEN","allowed_entities":["light.*"],"allowed_services":["light.turn_on"]}'
                    )
                )
                skills_result = asyncio.run(
                    controller.execute_command(
                        "/quick-skills " + json.dumps({"roots": [str(skill_root)]})
                    )
                )
                telegram_result = asyncio.run(
                    controller.execute_command(
                        "/quick-telegram "
                        + json.dumps(
                            {
                                "workspace": str(FIXTURE_ROOT),
                                "allowed_senders": ["123456"],
                                "token_env": "TELEGRAM_TEST_TOKEN",
                            }
                        )
                    )
                )
                slack_result = asyncio.run(
                    controller.execute_command(
                        '/quick-slack {"channel_id":"C01234567","token_env":"SLACK_TEST_TOKEN"}'
                    )
                )
                slack_inbound_result = asyncio.run(
                    controller.execute_command(
                        "/quick-channel "
                        + json.dumps(
                            {
                                "direction": "inbound",
                                "kind": "slack",
                                "fields": {
                                    "one": str(FIXTURE_ROOT),
                                    "two": "U123",
                                    "three": "C123",
                                    "four": "SLACK_SIGNING_SECRET_TEST",
                                },
                            }
                        )
                    )
                )
                schedule_result = asyncio.run(
                    controller.execute_command(
                        "/quick-schedule "
                        + json.dumps(
                            {
                                "goal": "Review the local repository once",
                                "every_minutes": 60,
                                "name": "Repository review",
                                "workspace": str(FIXTURE_ROOT),
                            }
                        )
                    )
                )
                container_result = asyncio.run(
                    controller.execute_command(
                        '/quick-container {"image":"python:3.12","programs":{"tests":["/usr/bin/python3"]},"docker_command":"docker"}'
                    )
                )
                remote_result = asyncio.run(
                    controller.execute_command(
                        "/quick-remote-worker "
                        + json.dumps(
                            {
                                "target_id": "build-worker",
                                "receipt": str(receipt_path),
                                "programs": {"tests": "/usr/bin/python3"},
                                "identity_file": None,
                            }
                        )
                    )
                )
                gateway_result = asyncio.run(
                    controller.execute_command('/gateway-service {"action":"status","receivers":[]}')
                )
                invalid_gateway_result = asyncio.run(
                    controller.execute_command('/gateway-service {"action":"status","receivers":["unknown"]}')
                )
                schedule_service_result = asyncio.run(
                    controller.execute_command('/schedule-service {"action":"status"}')
                )

                snapshot = controller.snapshot()

                self.assertEqual(controller.turn_count, 0)
                self.assertEqual(controller.input_history(), ())
                self.assertTrue(any(item.startswith("Manager") for item in snapshot.operating_report))
                self.assertTrue(any(item.startswith("Graph") for item in snapshot.operating_report))
                self.assertTrue(any(item.startswith("Budget") or item.startswith("Approval") for item in snapshot.operating_report))
            finally:
                controller.close()

            with KnowledgeStore(knowledge_state_path(state_path)) as store:
                records = store.list_records()
            from dynamic_firm.product.schedules import ScheduleStore

            with ScheduleStore(state_path) as schedule_store:
                schedules = schedule_store.list(include_disabled=True)
            persisted = _load_config(Path(temporary) / "config.toml")

        self.assertIn("Remembered locally", remembered.messages[0])
        self.assertTrue(any("Modern local Cedar note" in item for item in retrieved.messages))
        self.assertIn("/remember <text>", help_result.messages[0])
        self.assertIn("command palette", slash_result.messages[0])
        self.assertIn("Writes are disabled", tools_before.messages[1])
        self.assertIn("read-only → ask", permission_result.messages[0])
        self.assertIn("run.max_tool_calls → 21", limit_result.messages[0])
        self.assertIn("Connection saved", connection_result.messages[0])
        self.assertIn("Connection saved", custom_endpoint_result.messages[0])
        self.assertIn("Connection saved", model_only_result.messages[0])
        self.assertIn("Writes are enabled", tools_after.messages[1])
        self.assertTrue(settings_result.open_settings)
        self.assertTrue(graph_result.open_graph_controls)
        self.assertTrue(job_result.open_job_audit)
        self.assertEqual(job_snapshot["schema"], "noruct.job-audit-surface.v1")
        self.assertIsNone(job_snapshot["job"])
        self.assertIsNone(graph_before["selection"]["blueprint_id"])
        self.assertIn("Future Job Graph defaults saved", graph_saved[0])
        self.assertEqual(graph_after["selection"]["pinned_employee_ids"], ("employee-company-generalist",))
        self.assertTrue(graph_after["selection"]["require_independent_review"])
        self.assertEqual(graph_after["selection"]["mutation_policy"], "PROPOSE")
        self.assertTrue(any("Browser" in item for item in environment_settings_result.messages))
        self.assertTrue(any("local_browser" in item for item in capabilities_result.messages))
        self.assertIn("Web search connected", search_result.messages[0])
        self.assertIn("Browser connected", browser_result.messages[0])
        self.assertIn("Computer-use configured", computer_result.messages[0])
        self.assertIn("Media capabilities connected", media_result.messages[0])
        self.assertIn("Read-only MCP connected", mcp_result.messages[0])
        self.assertIn("MCP action connected", mcp_action_result.messages[0])
        self.assertIn("Home Assistant connected", home_assistant_result.messages[0])
        self.assertIn("External skill roots connected", skills_result.messages[0])
        self.assertIn("Telegram configured", telegram_result.messages[0])
        self.assertIn("Slack configured", slack_result.messages[0])
        self.assertIn("Inbound Slack configured", slack_inbound_result.messages[0])
        self.assertIn("Schedule schedule-", schedule_result.messages[0])
        self.assertIn("Container workspace configured", container_result.messages[0])
        self.assertIn("Remote worker configured", remote_result.messages[0])
        self.assertIn("Gateway service status", gateway_result.messages[0])
        self.assertIn("Gateway service was not changed", invalid_gateway_result.messages[0])
        self.assertIn("Schedule service status", schedule_service_result.messages[0])
        self.assertEqual([item.statement for item in records], ["Modern local Cedar note"])
        self.assertEqual(persisted["run"]["permission_mode"], "ask")
        self.assertEqual(persisted["run"]["max_tool_calls"], 21)
        self.assertEqual(persisted["provider"]["kind"], "openrouter")
        self.assertEqual(persisted["provider"]["base_url"], "https://gateway.example.test/v1")
        self.assertEqual(persisted["provider"]["model"], "openrouter/final-contract")
        self.assertEqual(persisted["provider"]["request_timeout"], 45.0)
        self.assertEqual(persisted["web_search"]["base_url"], "http://127.0.0.1:8080")
        self.assertEqual(persisted["browser"]["cdp_endpoint"], "http://127.0.0.1:9222")
        self.assertTrue(persisted["browser"]["allow_control"])
        self.assertEqual(persisted["computer_use"]["allowed_apps"], ["Finder"])
        self.assertTrue(persisted["computer_use"]["allow_control"])
        self.assertTrue(persisted["openai_media"]["image_enabled"])
        self.assertTrue(persisted["openai_media"]["speech_enabled"])
        self.assertEqual(persisted["mcp"]["profile"], "settings-mcp")
        self.assertEqual(persisted["mcp"]["tool_names"], ["read_context"])
        self.assertEqual(persisted["mcp_action"]["profile"], "settings-mcp-action")
        self.assertEqual(persisted["home_assistant"]["allowed_entities"], ["light.*"])
        self.assertEqual(persisted["skills"]["external_dirs"], [str(skill_root.resolve())])
        self.assertEqual(persisted["telegram_channel"]["allowed_senders"], ["123456"])
        self.assertEqual(persisted["slack_channel"]["channel_id"], "C01234567")
        self.assertEqual(persisted["slack_inbound"]["allowed_channels"], ["C123"])
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0].name, "Repository review")
        self.assertEqual(persisted["container"]["programs"]["tests"], ["/usr/bin/python3"])
        self.assertEqual(persisted["remote_worker"]["target_id"], "build-worker")

    def test_modern_settings_employee_revision_creates_only_a_roster_proposal(self) -> None:
        import asyncio

        from dynamic_firm.cli import _ModernInteractiveController

        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            args = build_parser().parse_args(
                [
                    "--config", str(Path(temporary) / "config.toml"),
                    "chat", "--workspace", str(FIXTURE_ROOT),
                    "--state", str(state_path), "--provider", "openai-api",
                    "--base-url", "http://127.0.0.1:9/v1", "--model", "contract-model",
                    "--no-auth", "--permission-mode", "read-only",
                ]
            )
            controller = _ModernInteractiveController(
                args,
                {},
                provider_factory=lambda _config: self.fail("Settings must not build a provider"),
                coding_worker_factory=lambda _config: self.fail("Settings must not build a coding worker"),
            )
            try:
                employee = next(
                    item
                    for item in controller.roster_snapshot.employees
                    if MANAGER_CAPABILITY not in item.capabilities
                )
                result = asyncio.run(
                    controller.execute_command(
                        "/company-employee-revise " + json.dumps(
                            {
                                "employee_id": employee.employee_id,
                                "role": employee.role,
                                "capabilities": list(employee.capabilities) + ["settings-check"],
                                "model_profile": "settings-contract-model",
                                "rationale": "Validate the Settings-only proposed ROSTER lifecycle.",
                            }
                        )
                    )
                )
                manager = next(
                    item
                    for item in controller.roster_snapshot.employees
                    if MANAGER_CAPABILITY in item.capabilities
                )
                manager_result = asyncio.run(
                    controller.execute_command(
                        "/company-manager-revise " + json.dumps(
                            {
                                "role": manager.role,
                                "model_profile": "settings-manager-model",
                                "rationale": "Validate the distinct Manager ROSTER proposal lifecycle.",
                            }
                        )
                    )
                )
                skill_result = asyncio.run(
                    controller.execute_command(
                        "/company-skill-propose " + json.dumps(
                            {
                                "employee_id": employee.employee_id,
                                "skill_key": "settings-check",
                                "context_key": "qualification",
                                "purpose": "Keep Settings lifecycle evidence bounded.",
                                "steps": ["Inspect the requested Settings scope."],
                                "verification_steps": ["Confirm the active procedure is unchanged before approval."],
                                "prohibitions": ["Do not apply the patch automatically."],
                                "correction_id": "settings-contract-correction-1",
                                "rationale": "Validate Skill proposal-only behavior from Settings.",
                            }
                        )
                    )
                )
                self.assertIn("ROSTER Patch proposed", "\n".join(result.messages))
                self.assertIn("Manager ROSTER Patch proposed", "\n".join(manager_result.messages))
                self.assertIn("Employee Skill Patch proposed", "\n".join(skill_result.messages))
                with CompanyStateStore(state_path) as store:
                    self.assertEqual(store.roster().revision, controller.roster_snapshot.revision)
                    patches = store.list_roster_patches()
                    skill_patches = store.list_employee_skill_patches()
                self.assertEqual(len(patches), 2)
                self.assertTrue(all(patch.status.value == "PROPOSED" for patch in patches))
                self.assertTrue(any(
                    patch.after_employee["model_profile"] == "settings-contract-model"
                    for patch in patches
                ))
                manager_patch = next(
                    patch for patch in patches
                    if patch.after_employee["model_profile"] == "settings-manager-model"
                )
                self.assertEqual(manager_patch.after_employee["capabilities"], list(manager.capabilities))
                self.assertEqual(len(skill_patches), 1)
                self.assertEqual(skill_patches[0].status.value, "PROPOSED")
            finally:
                controller.close()

    def test_module_and_repo_wrapper_expose_the_same_product_entrypoint(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        module = subprocess.run(
            [sys.executable, "-m", "dynamic_firm", "--help"],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        wrapper = subprocess.run(
            [str(PROJECT_ROOT / "bin" / "noruct"), "--help"],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(module.returncode, 0, module.stderr)
        self.assertEqual(wrapper.returncode, 0, wrapper.stderr)
        self.assertIn("Give one goal", module.stdout)
        self.assertEqual(module.stdout, wrapper.stdout)

    def test_run_assembles_solo_company_and_prints_stable_json(self) -> None:
        provider = ScriptedModelProvider(
            [
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="The repository evidence supports the requested answer.",
                        acceptance_evidence=("calculator.py:1",),
                        unresolved_issues=("No live model was used in this contract test.",),
                    )
                )
            ]
        )
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            secret = "must-never-be-persisted-or-printed"
            old_secret = os.environ.get("CLI_TEST_SECRET")
            os.environ["CLI_TEST_SECRET"] = secret
            try:
                exit_code = main(
                    [
                        "run",
                        "Inspect the repository",
                        "--workspace",
                        str(FIXTURE_ROOT),
                        "--state",
                        str(Path(temporary) / "runtime.db"),
                        "--base-url",
                        "http://127.0.0.1:9/v1",
                        "--model",
                        "contract-model",
                        "--api-key-env",
                        "CLI_TEST_SECRET",
                        "--json",
                    ],
                    provider_factory=lambda config: provider,
                    stdout=output,
                    stderr=error,
                )
            finally:
                if old_secret is None:
                    os.environ.pop("CLI_TEST_SECRET", None)
                else:
                    os.environ["CLI_TEST_SECRET"] = old_secret

            payload = json.loads(output.getvalue())
            database_bytes = (Path(temporary) / "runtime.db").read_bytes()

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["status"], "SUCCEEDED")
        self.assertEqual(payload["planning_mode"], "SOLO")
        self.assertEqual(payload["planning_reason"], "SOLO_FIRST_ATTEMPT")
        self.assertEqual(payload["compiler_usage"]["model_calls"], 0)
        self.assertEqual(payload["initial_company_work_mode"], "SOLO_JOB")
        self.assertEqual(payload["company_work_mode"], "SOLO_JOB")
        self.assertEqual(payload["coordination_policy"], "SOLO_FIRST")
        self.assertTrue(payload["work_order_id"].startswith("work-order-"))
        self.assertEqual(len(payload["work_order_digest"]), 64)
        self.assertEqual(len(payload["work_order_authority_digest"]), 64)
        self.assertEqual(provider.requests[0].model_profile, "contract-model")
        self.assertTrue(
            any(
                "Inspect the repository" in str(message.content)
                for message in provider.requests[0].messages
            )
        )
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn(secret.encode(), database_bytes)

    def test_explicit_multi_workstream_goal_compiles_and_runs_minimal_team(self) -> None:
        provider = TeamStructuredProvider()
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            exit_code = main(
                [
                    "run",
                    (
                        "Form a team and in parallel analyze the market and inspect the "
                        "repository, then independently integrate the findings."
                    ),
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(Path(temporary) / "runtime.db"),
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "contract-model",
                    "--no-auth",
                    "--json",
                ],
                provider_factory=lambda config: provider,
                stdout=output,
                stderr=error,
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["status"], "SUCCEEDED")
        self.assertEqual(payload["planning_mode"], "DYNAMIC")
        self.assertEqual(payload["planning_reason"], "VALID_DYNAMIC")
        self.assertEqual(payload["compiler_usage"]["model_calls"], 1)
        self.assertEqual(payload["initial_company_work_mode"], "SOLO_JOB")
        self.assertEqual(payload["company_work_mode"], "TEAM_JOB")
        self.assertEqual(payload["coordination_policy"], "SOLO_FIRST")
        self.assertTrue(payload["work_order_id"].startswith("work-order-"))
        self.assertEqual(len(payload["work_order_digest"]), 64)
        self.assertEqual(payload["metrics"]["unique_employee_count"], 2)
        self.assertEqual(payload["metrics"]["manager_integration_count"], 0)
        self.assertEqual(payload["metrics"]["maximum_parallelism"], 2)
        self.assertEqual(len(payload["final_tasks"]), 3)
        self.assertEqual(
            payload["summary"],
            "The company integrated both independent findings.",
        )
        self.assertEqual(len(provider.structured_requests), 1)
        self.assertEqual(len(provider.requests), 3)
        specialist_tool_sets = (
            {tool.name for tool in provider.requests[0].tools},
            {tool.name for tool in provider.requests[1].tools},
        )
        final_employee_tool_set = {tool.name for tool in provider.requests[2].tools}
        self.assertTrue(
            all(
                not any(name.startswith("manager_") for name in tool_names)
                for tool_names in specialist_tool_sets
            )
        )
        self.assertEqual(
            {name for name in final_employee_tool_set if name.startswith("manager_")},
            set(),
        )
        employee_prompt = "\n".join(
            str(message.content)
            for request in provider.requests
            for message in request.messages
        )
        self.assertIn(
            "Complete user goals through the smallest sufficient AI company.",
            employee_prompt,
        )

    def test_company_budget_denial_happens_before_team_compiler_call(self) -> None:
        provider = TeamStructuredProvider()
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "runtime.db"
            with CompanyStateStore(state) as company:
                company.set_company_cost_budget_policy(
                    {
                        "max_total_cost_usd": 0.5,
                        "window_kind": "lifetime",
                    },
                    actor="test:company-front-door",
                )
            exit_code = main(
                [
                    "run",
                    (
                        "Form a team and in parallel analyze the market and inspect "
                        "the repository, then independently integrate the findings."
                    ),
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(state),
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "contract-model",
                    "--no-auth",
                    "--max-cost-usd",
                    "1.0",
                    "--json",
                ],
                provider_factory=lambda config: provider,
                stdout=output,
                stderr=error,
            )
            store = RunStore(state)
            try:
                inspection = ActiveJobInspector(store).list(1)[0]
            finally:
                store.close()

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_JOB_FAILED, error.getvalue())
        self.assertEqual(payload["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(provider.structured_requests, [])
        self.assertEqual(provider.requests, [])
        self.assertEqual(inspection.job_status, "BUDGET_EXHAUSTED")

    def test_direct_goal_shorthand_runs_without_the_run_subcommand(self) -> None:
        provider = ScriptedModelProvider(
            [
                ModelResponse(
                    completion=CompletionEnvelope(summary="Direct goal completed.")
                )
            ]
        )
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            exit_code = main(
                [
                    "Inspect",
                    "the",
                    "repository",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(Path(temporary) / "runtime.db"),
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--provider",
                    "openai-api",
                    "--model",
                    "contract-model",
                    "--no-auth",
                ],
                provider_factory=lambda config: provider,
                stdout=output,
                stderr=error,
            )

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertIn("Direct goal completed.", output.getvalue())
        self.assertTrue(
            any(
                "Inspect the repository" in str(message.content)
                for message in provider.requests[0].messages
            )
        )

    def test_question_shorthand_uses_one_direct_model_call(self) -> None:
        provider = ScriptedModelProvider(
            [ModelResponse(completion=CompletionEnvelope(summary="안녕! 나는 Noruct야."))]
        )
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            exit_code = main(
                [
                    "hello",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(Path(temporary) / "runtime.db"),
                    "--provider",
                    "openai-api",
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "contract-model",
                    "--no-auth",
                ],
                provider_factory=lambda config: provider,
                stdout=output,
                stderr=error,
            )

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(
            {tool.name for tool in provider.requests[0].tools},
            {
                "manager_inspect_company",
                "manager_inspect_current_job",
                "manager_read_intent_brief",
                "manager_review_recent_outcomes",
            },
        )
        self.assertIn("안녕! 나는 Noruct야.", output.getvalue())

    def test_persisted_active_roster_is_compiler_and_kernel_authority(self) -> None:
        provider = RosterCapturingProvider()
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            with CompanyStateStore(state_path) as store:
                seeded = store.ensure_roster_baseline(
                    (
                        EmployeeRecord(
                            employee_id="employee-persistent-specialist",
                            role="Persistent Specialist",
                            capabilities=("persistent_capability",),
                            model_profile="roster-default",
                        ),
                        EmployeeRecord(
                            employee_id="employee-dormant-specialist",
                            role="Dormant Specialist",
                            capabilities=("dormant_capability",),
                            active=False,
                            model_profile="roster-default",
                        ),
                    )
                )
            exit_code = main(
                [
                    "run",
                    "Use the persistent specialist",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(state_path),
                    "--provider",
                    "openai-api",
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "session-model",
                    "--no-auth",
                ],
                provider_factory=lambda config: provider,
                stdout=output,
                stderr=error,
            )
            with CompanyStateStore(state_path) as store:
                persisted = store.roster()
            status_output = io.StringIO()
            status_code = main(
                [
                    "--config",
                    str(Path(temporary) / "missing.toml"),
                    "company",
                    "status",
                    "--state",
                    str(state_path),
                    "--json",
                ],
                stdout=status_output,
                stderr=error,
            )
            company_status = json.loads(status_output.getvalue())

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(status_code, EXIT_OK, error.getvalue())
        self.assertEqual(seeded.revision, 2)
        self.assertEqual(provider.structured_requests, [])
        employee_prompt = str(provider.requests[0].messages)
        self.assertIn("employee-persistent-specialist", employee_prompt)
        self.assertIn("Persistent Specialist", employee_prompt)
        self.assertIn("persistent_capability", employee_prompt)
        self.assertNotIn("dormant_capability", employee_prompt)
        self.assertEqual(provider.requests[0].model_profile, "session-model")
        self.assertEqual(persisted.employees[0]["model_profile"], "roster-default")
        self.assertEqual(company_status["summary"]["active_employee_count"], 1)
        self.assertEqual(company_status["summary"]["employee_count"], 2)

    def test_chat_resume_reuses_persistent_employee_identity_and_roster_revision(self) -> None:
        first_provider = ScriptedModelProvider(
            [ModelResponse(completion=CompletionEnvelope(summary="First response."))]
        )
        second_provider = ScriptedModelProvider(
            [ModelResponse(completion=CompletionEnvelope(summary="Resumed response."))]
        )
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            with CompanyStateStore(state_path) as store:
                store.ensure_roster_baseline(
                    (
                        EmployeeRecord(
                            employee_id="employee-conversation-veteran",
                            role="Conversation Veteran",
                            capabilities=("conversation",),
                            model_profile="roster-default",
                        ),
                    )
                )
            common = [
                "--state",
                str(state_path),
                "--provider",
                "openai-api",
                "--base-url",
                "http://127.0.0.1:9/v1",
                "--model",
                "session-model",
                "--no-auth",
            ]
            first_output = TtyStringIO()
            first_exit = main(
                ["chat", "--workspace", str(FIXTURE_ROOT), *common],
                provider_factory=lambda config: first_provider,
                stdin=TtyStringIO("hello\n/quit\n"),
                stdout=first_output,
                stderr=error,
            )
            sessions = CompanySessionStore(state_path)
            try:
                session_id = sessions.list(1)[0].session_id
            finally:
                sessions.close()
            second_output = TtyStringIO()
            second_exit = main(
                ["resume", session_id[:10], *common],
                provider_factory=lambda config: second_provider,
                stdin=TtyStringIO("hello again\n/quit\n"),
                stdout=second_output,
                stderr=error,
            )

        self.assertEqual(first_exit, EXIT_OK, error.getvalue())
        self.assertEqual(second_exit, EXIT_OK, error.getvalue())
        for provider in (first_provider, second_provider):
            self.assertIn("employee-conversation-veteran", str(provider.requests[0].messages))
            self.assertEqual(provider.requests[0].model_profile, "session-model")
        self.assertIn("roster        r2 · 1 active", first_output.getvalue())
        self.assertIn("roster        r2 · 1 active", second_output.getvalue())

    def test_setup_writes_config_without_accepting_a_secret_value(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            exit_code = main(
                [
                    "--config",
                    str(config_path),
                    "setup",
                    "--base-url",
                    "http://127.0.0.1:11434/v1",
                    "--model",
                    "local-model",
                    "--api-key-env",
                    "LOCAL_MODEL_KEY",
                ],
                stdout=output,
                stderr=error,
            )
            payload = tomllib.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["provider"]["model"], "local-model")
        self.assertEqual(payload["provider"]["api_key_env"], "LOCAL_MODEL_KEY")
        self.assertNotIn("api_key", payload["provider"])

    def test_first_interactive_run_opens_connection_wizard_and_enters_local_chat(self) -> None:
        output = TtyStringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            exit_code = main(
                ["--config", str(config_path), "chat"],
                stdin=TtyStringIO("9\n\nlocal-contract-model\n/quit\n"),
                stdout=output,
                stderr=error,
            )
            payload = tomllib.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["provider"]["kind"], "ollama")
        self.assertEqual(payload["provider"]["model"], "local-contract-model")
        self.assertTrue(payload["provider"]["no_auth"])
        self.assertIn("First run · connect your company", output.getvalue())
        self.assertIn("Ollama (local)", output.getvalue())

    def test_first_interactive_run_saves_api_selection_without_launching_unready_chat(self) -> None:
        output = TtyStringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            exit_code = main(
                ["--config", str(config_path), "chat"],
                stdin=TtyStringIO("2\n\ngpt-contract-model\n"),
                stdout=output,
                stderr=error,
            )
            payload = tomllib.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["provider"]["kind"], "openai_api")
        self.assertEqual(payload["provider"]["base_url"], "https://api.openai.com/v1")
        self.assertEqual(payload["provider"]["api_key_env"], "OPENAI_API_KEY")
        self.assertIn("Finish the external login or set the named API-key", output.getvalue())

    def test_tools_status_is_the_primary_capability_summary(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        exit_code = main(["tools", "status"], stdout=output, stderr=error)

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertIn("Noruct tools · configured surfaces", output.getvalue())

    def test_interactive_company_persists_turns_and_injects_bounded_prior_context(self) -> None:
        provider = ScriptedModelProvider(
            [
                ModelResponse(
                    completion=CompletionEnvelope(summary="First turn result.")
                ),
                ModelResponse(
                    completion=CompletionEnvelope(summary="Second turn result.")
                ),
            ]
        )
        input_stream = TtyStringIO("Inspect the module\nNow inspect its tests\n/quit\n")
        output = TtyStringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                "[provider]\n"
                'base_url = "http://127.0.0.1:9/v1"\n'
                'model = "contract-model"\n'
                "no_auth = true\n\n"
                "[run]\n"
                f'state = "{state_path}"\n',
                encoding="utf-8",
            )
            previous = os.environ.get("NORUCT_CONFIG")
            os.environ["NORUCT_CONFIG"] = str(config_path)
            try:
                exit_code = main(
                    [],
                    provider_factory=lambda config: provider,
                    stdin=input_stream,
                    stdout=output,
                    stderr=error,
                )
            finally:
                if previous is None:
                    os.environ.pop("NORUCT_CONFIG", None)
                else:
                    os.environ["NORUCT_CONFIG"] = previous
            store = CompanySessionStore(state_path)
            try:
                saved = store.list(5)
            finally:
                store.close()

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(saved[0].turn_count, 2)
        self.assertIn("NORUCT", output.getvalue())
        self.assertIn("Second turn result.", output.getvalue())
        second_messages = str(provider.requests[1].messages)
        self.assertIn("Prior company turn 1", second_messages)
        self.assertIn("First turn result.", second_messages)

    def test_interactive_question_skips_compiler_but_keeps_ask_mode_tools_available(self) -> None:
        provider = ScriptedModelProvider(
            [ModelResponse(completion=CompletionEnvelope(summary="나는 Noruct야."))]
        )
        input_stream = TtyStringIO("이름이 뭐야?\n/quit\n")
        output = TtyStringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                "[provider]\n"
                'base_url = "http://127.0.0.1:9/v1"\n'
                'model = "contract-model"\n'
                "no_auth = true\n\n"
                "[run]\n"
                f'state = "{state_path}"\n',
                encoding="utf-8",
            )
            exit_code = main(
                ["--config", str(config_path), "chat", "--workspace", str(FIXTURE_ROOT)],
                provider_factory=lambda config: provider,
                stdin=input_stream,
                stdout=output,
                stderr=error,
            )
            store = CompanySessionStore(state_path)
            try:
                saved = store.list(1)
            finally:
                store.close()
            connection = sqlite3.connect(state_path)
            try:
                job_snapshot_count = int(
                    connection.execute("SELECT COUNT(*) FROM job_snapshots").fetchone()[0]
                )
                job_terminal_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM job_terminal_events"
                    ).fetchone()[0]
                )
                employee_run_count = int(
                    connection.execute("SELECT COUNT(*) FROM employee_runs").fetchone()[0]
                )
            finally:
                connection.close()

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(provider.call_count, 1)
        # Direct chat remains a one-agent turn with no Compiler, while ask
        # authority discloses bounded tools so wording cannot make the agent
        # falsely report that a requested local action is unavailable.
        self.assertIn(
            "run_workspace_command",
            {tool.name for tool in provider.requests[0].tools},
        )
        self.assertTrue(
            any(
                "Answer the user's message directly as Noruct" in str(message.content)
                for message in provider.requests[0].messages
            )
        )
        self.assertIn("나는 Noruct야.", output.getvalue())
        self.assertNotIn("Company plan", output.getvalue())
        self.assertNotIn("Compiler", output.getvalue())
        self.assertEqual(saved[0].turn_count, 1)
        self.assertEqual(job_snapshot_count, 0)
        self.assertEqual(job_terminal_count, 0)
        self.assertEqual(employee_run_count, 1)

    def test_interactive_local_commands_need_no_model_call(self) -> None:
        input_stream = TtyStringIO(
            "/remember The local codename is Cedar.\n"
            "/knowledge codename\n/intent\n/decision due\n"
            "/status\n/details on\n/view expand\n/usage\n/help\n/clear\n/quit\n"
        )
        output = TtyStringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                "[provider]\n"
                'base_url = "http://127.0.0.1:9/v1"\n'
                'model = "contract-model"\n'
                "no_auth = true\n\n"
                "[run]\n"
                f'state = "{state_path}"\n',
                encoding="utf-8",
            )
            exit_code = main(
                ["--config", str(config_path), "chat", "--workspace", str(FIXTURE_ROOT)],
                provider_factory=lambda config: self.fail("local commands must not build a provider"),
                stdin=input_stream,
                stdout=output,
                stderr=error,
            )
            from dynamic_firm.knowledge.store import KnowledgeStore, knowledge_state_path

            with KnowledgeStore(knowledge_state_path(state_path)) as knowledge_store:
                records = knowledge_store.list_records()
            sessions = CompanySessionStore(state_path)
            try:
                saved_session = sessions.list(1)[0]
            finally:
                sessions.close()
            import sqlite3

            runtime_db = sqlite3.connect(state_path)
            try:
                persisted_company_turns = runtime_db.execute(
                    "SELECT COUNT(*) FROM company_turns"
                ).fetchone()[0]
                persisted_employee_runs = runtime_db.execute(
                    "SELECT COUNT(*) FROM employee_runs"
                ).fetchone()[0]
                persisted_run_messages = runtime_db.execute(
                    "SELECT COUNT(*) FROM run_messages"
                ).fetchone()[0]
            finally:
                runtime_db.close()

        rendered = output.getvalue()
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertIn("status", rendered)
        self.assertIn("Execution details: expanded", rendered)
        self.assertIn("Live dock: expanded", rendered)
        self.assertIn("Session usage", rendered)
        self.assertIn("Remembered locally", rendered)
        self.assertIn("Knowledge view · 1 match", rendered)
        self.assertIn("Active intents · none", rendered)
        self.assertIn("Decisions due for review · none", rendered)
        self.assertIn("/details [on|off]", rendered)
        self.assertIn("/view [expand|collapse]", rendered)
        self.assertIn("/remember <text>", rendered)
        self.assertEqual([item.statement for item in records], ["The local codename is Cedar."])
        self.assertEqual(saved_session.turn_count, 0)
        self.assertEqual(
            (persisted_company_turns, persisted_employee_runs, persisted_run_messages),
            (0, 0, 0),
        )
        self.assertIn(CLEAR_SCREEN, rendered)

    def test_interactive_review_picker_versions_company_policy_without_model_call(self) -> None:
        input_stream = TtyStringIO("/review\n2\n/status\n/quit\n")
        output = TtyStringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                "[provider]\n"
                'base_url = "http://127.0.0.1:9/v1"\n'
                'model = "contract-model"\n'
                "no_auth = true\n\n"
                "[run]\n"
                f'state = "{state_path}"\n',
                encoding="utf-8",
            )
            exit_code = main(
                ["--config", str(config_path), "chat", "--workspace", str(FIXTURE_ROOT)],
                provider_factory=lambda config: self.fail("review picker must not build a provider"),
                stdin=input_stream,
                stdout=output,
                stderr=error,
            )
            with CompanyStateStore(state_path) as store:
                mode = store.retention_review_mode().value
                revision = store.company().revision

        rendered = output.getvalue()
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(mode, "auto-review")
        self.assertEqual(revision, 2)
        self.assertIn("SELECT REVIEW MODE", rendered)
        self.assertIn("approval → auto-review", rendered)
        self.assertIn("review", rendered)

    def test_interactive_model_switch_updates_next_turn_and_session_without_extra_call(self) -> None:
        provider = ScriptedModelProvider(
            [ModelResponse(completion=CompletionEnvelope(summary="Switched model response."))]
        )
        provider_models: list[str] = []
        input_stream = TtyStringIO("/model replacement-model\nhello\n/quit\n")
        output = TtyStringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                "[provider]\n"
                'base_url = "http://127.0.0.1:9/v1"\n'
                'model = "contract-model"\n'
                "no_auth = true\n\n"
                "[run]\n"
                f'state = "{state_path}"\n',
                encoding="utf-8",
            )

            def provider_factory(config):
                provider_models.append(config.model)
                return provider

            exit_code = main(
                ["--config", str(config_path), "chat", "--workspace", str(FIXTURE_ROOT)],
                provider_factory=provider_factory,
                stdin=input_stream,
                stdout=output,
                stderr=error,
            )
            store = CompanySessionStore(state_path)
            try:
                saved = store.list(1)
            finally:
                store.close()

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(provider_models, ["replacement-model"])
        self.assertEqual(saved[0].model, "replacement-model")
        self.assertIn("Model switched · contract-model → replacement-model", output.getvalue())

    def test_interactive_plain_mode_has_no_ansi_or_box_drawing(self) -> None:
        output = TtyStringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                "[provider]\n"
                'base_url = "http://127.0.0.1:9/v1"\n'
                'model = "contract-model"\n'
                "no_auth = true\n\n"
                "[run]\n"
                f'state = "{state_path}"\n',
                encoding="utf-8",
            )
            exit_code = main(
                [
                    "--config",
                    str(config_path),
                    "chat",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--plain",
                ],
                provider_factory=lambda config: self.fail("quit must not build a provider"),
                stdin=TtyStringIO("/quit\n"),
                stdout=output,
                stderr=error,
            )

        rendered = output.getvalue()
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertNotIn("\x1b", rendered)
        for character in "╭╮╰╯├┤│─":
            self.assertNotIn(character, rendered)
        self.assertIn("Noruct", rendered)

    def test_sessions_list_and_resume_use_the_company_session_ledger(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            store = CompanySessionStore(state_path)
            try:
                session = store.create(
                    workspace=workspace,
                    model="contract-model",
                    title="Resume contract",
                )
            finally:
                store.close()

            list_exit = main(
                ["sessions", "--state", str(state_path), "--json"],
                stdout=output,
                stderr=error,
            )
            listed = json.loads(output.getvalue())
            resume_output = TtyStringIO()
            resume_exit = main(
                [
                    "resume",
                    session.session_id[:10],
                    "--state",
                    str(state_path),
                    "--model",
                    "initial-local-model",
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--no-auth",
                ],
                stdin=TtyStringIO("/quit\n"),
                stdout=resume_output,
                stderr=error,
            )

        self.assertEqual(list_exit, EXIT_OK, error.getvalue())
        self.assertEqual(resume_exit, EXIT_OK, error.getvalue())
        self.assertEqual(listed[0]["session_id"], session.session_id)
        self.assertIn("workspace", resume_output.getvalue())
        self.assertIn(workspace.name, resume_output.getvalue())
        self.assertIn(session.session_id[:12], resume_output.getvalue())

    def test_chat_sessions_command_keeps_persisted_roster_model_and_workspace(self) -> None:
        provider = ScriptedModelProvider(
            [ModelResponse(completion=CompletionEnvelope(summary="Resumed local answer."))]
        )
        output = TtyStringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            store = CompanySessionStore(state_path)
            try:
                target = store.create(
                    workspace=FIXTURE_ROOT,
                    model="resumed-local-model",
                    title="Resume from terminal",
                )
            finally:
                store.close()
            exit_code = main(
                [
                    "chat",
                    "--state",
                    str(state_path),
                    "--model",
                    "initial-local-model",
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--no-auth",
                ],
                provider_factory=lambda config: provider,
                stdin=TtyStringIO(f"/sessions {target.session_id[:12]}\nhello\n/quit\n"),
                stdout=output,
                stderr=error,
            )

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertIn("Resuming company session", output.getvalue())
        self.assertIn(target.session_id[:12], output.getvalue())
        self.assertEqual(provider.requests[0].model_profile, "initial-local-model")

    def test_chat_session_resume_restores_bound_provider_not_current_cli_transport(self) -> None:
        provider = ScriptedModelProvider(
            [ModelResponse(completion=CompletionEnvelope(summary="Bound transport answer."))]
        )
        seen_configs = []
        output = TtyStringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            store = CompanySessionStore(state_path)
            try:
                target = store.create(
                    workspace=FIXTURE_ROOT,
                    model="bound-anthropic-model",
                    title="Bound provider session",
                    provider_kind="anthropic_api",
                    provider_base_url="https://api.example.invalid/v1",
                    provider_api_key_env="BOUND_PROVIDER_KEY",
                )
            finally:
                store.close()
            exit_code = main(
                [
                    "chat",
                    "--state",
                    str(state_path),
                    "--provider",
                    "openai-api",
                    "--model",
                    "current-openai-model",
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--no-auth",
                ],
                provider_factory=lambda config: (seen_configs.append(config) or provider),
                stdin=TtyStringIO(f"/sessions {target.session_id[:12]}\nhello\n/quit\n"),
                stdout=output,
                stderr=error,
            )

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(len(seen_configs), 1)
        self.assertIsInstance(seen_configs[0], AnthropicProviderConfig)
        bound = seen_configs[0]
        assert isinstance(bound, AnthropicProviderConfig)
        self.assertEqual(bound.model, "bound-anthropic-model")
        self.assertEqual(bound.base_url, "https://api.example.invalid/v1")
        self.assertEqual(bound.api_key_env, "BOUND_PROVIDER_KEY")

    def test_bound_company_session_refuses_changed_mcp_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            store = CompanySessionStore(state_path)
            try:
                session = store.create(
                    workspace=FIXTURE_ROOT,
                    model="bound-model",
                    provider_kind="openai_api",
                    provider_base_url="http://127.0.0.1:9/v1",
                    mcp_binding_digest="a" * 64,
                )
            finally:
                store.close()
            args = build_parser().parse_args(
                [
                    "chat",
                    "--state",
                    str(state_path),
                    "--model",
                    "other-model",
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--no-auth",
                ]
            )
            with self.assertRaisesRegex(ValueError, "original MCP configuration"):
                _activate_interactive_session(args, {}, session)

    def test_interactive_write_requires_visible_approval_before_mutation(self) -> None:
        provider = ScriptedModelProvider(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "write-1",
                            "write_workspace_file",
                            {
                                "workspace_id": "noruct-workspace",
                                "path": "created.py",
                                "content": "value = 1\n",
                            },
                        ),
                    )
                ),
                ModelResponse(
                    completion=CompletionEnvelope(summary="Created the requested file.")
                ),
            ]
        )
        input_stream = TtyStringIO("1\n")
        output = TtyStringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            exit_code = main(
                [
                    "run",
                    "Create a small Python file",
                    "--workspace",
                    str(workspace),
                    "--state",
                    str(Path(temporary) / "runtime.db"),
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--provider",
                    "openai-api",
                    "--model",
                    "contract-model",
                    "--no-auth",
                    "--permission-mode",
                    "ask",
                    "--trust-mode",
                    "strict",
                ],
                provider_factory=lambda config: provider,
                stdin=input_stream,
                stdout=output,
                stderr=error,
            )
            written = (workspace / "created.py").read_text(encoding="utf-8")

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(written, "value = 1\n")
        self.assertIn("APPROVAL · REQUIRED", output.getvalue())
        self.assertIn("Write created.py", output.getvalue())
        self.assertIn("Created the requested file.", output.getvalue())

    def test_codex_ask_mode_uses_shadow_for_explicit_broad_refactor(self) -> None:
        provider = SoloStructuredProvider()
        worker = CliShadowWorker()
        input_stream = TtyStringIO("1\n")
        output = TtyStringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            exit_code = main(
                [
                    "run",
                    "Refactor the architecture across multiple files",
                    "--workspace",
                    str(workspace),
                    "--state",
                    str(Path(temporary) / "runtime.db"),
                    "--provider",
                    "openai-codex",
                    "--codex-command",
                    "user-managed-codex",
                    "--permission-mode",
                    "ask",
                    "--trust-mode",
                    "strict",
                ],
                provider_factory=lambda config: provider,
                coding_worker_factory=lambda config: worker,
                stdin=input_stream,
                stdout=output,
                stderr=error,
            )
            self.assertEqual(exit_code, EXIT_OK, error.getvalue())
            written = (workspace / "created.py").read_text(encoding="utf-8")

        self.assertEqual(written, "value = 2\n")
        self.assertEqual(len(worker.workspaces), 1)
        self.assertNotEqual(worker.workspaces[0], workspace)
        self.assertIn("openai-codex (external)", output.getvalue())
        self.assertIn("shadow-only worker", output.getvalue())
        self.assertIn("APPROVAL · REQUIRED", output.getvalue())
        self.assertIn("created.py", output.getvalue())

    def test_non_tty_cannot_request_mutation_authority(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            exit_code = main(
                [
                    "run",
                    "Change a file",
                    "--workspace",
                    temporary,
                    "--state",
                    str(Path(temporary) / "runtime.db"),
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "contract-model",
                    "--no-auth",
                    "--permission-mode",
                    "ask",
                ],
                provider_factory=lambda config: self.fail("provider must not be constructed"),
                stdout=output,
                stderr=error,
            )

        self.assertEqual(exit_code, EXIT_INPUT)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("requires an interactive", error.getvalue())

    def test_run_reaches_local_openai_compatible_provider_end_to_end(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, contract_server(
            {"json": completion_body(summary="Local provider completed the company goal.")},
        ) as (server, base_url):
            exit_code = main(
                [
                    "run",
                    "Inspect the repository through the provider contract",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(Path(temporary) / "runtime.db"),
                    "--base-url",
                    base_url,
                    "--model",
                    "contract-model",
                    "--no-auth",
                    "--json",
                ],
                stdout=output,
                stderr=error,
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["status"], "SUCCEEDED")
        self.assertEqual(payload["planning_mode"], "SOLO")
        self.assertEqual(payload["planning_reason"], "SOLO_FIRST_ATTEMPT")
        self.assertEqual(payload["metrics"]["usage"]["model_calls"], 1)
        self.assertEqual(payload["compiler_usage"]["model_calls"], 0)
        self.assertEqual(len(server.captures), 1)
        self.assertEqual(server.captures[0]["path"], "/v1/chat/completions")
        self.assertIsNone(server.captures[0]["authorization"])
        self.assertIn(
            "Inspect the repository through the provider contract",
            str(server.captures[0]["body"]["messages"]),
        )
        self.assertEqual(
            server.captures[0]["body"]["response_format"]["json_schema"]["name"],
            "dynamic_firm_employee_completion",
        )

    def test_typed_capability_gap_expands_solo_into_specialist_and_integrator(self) -> None:
        gap = {
            "code": "CAPABILITY_MISSING",
            "value": "security_review",
            "evidence": ["repository evidence crossed a specialist boundary"],
        }
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, contract_server(
            {
                "json": completion_body(
                    summary="Repository evidence",
                    signals=(gap,),
                )
            },
            {"json": completion_body(summary="Security evidence")},
            {"json": completion_body(summary="Integrated company result")},
        ) as (server, base_url):
            exit_code = main(
                [
                    "run",
                    "Inspect code and tests, then integrate the findings",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(Path(temporary) / "runtime.db"),
                    "--base-url",
                    base_url,
                    "--model",
                    "contract-model",
                    "--no-auth",
                    "--json",
                ],
                stdout=output,
                stderr=error,
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["status"], "SUCCEEDED")
        self.assertEqual(payload["planning_mode"], "SOLO")
        self.assertEqual(payload["planning_reason"], "SOLO_FIRST_ATTEMPT")
        self.assertEqual(payload["metrics"]["maximum_parallelism"], 1)
        self.assertEqual(payload["metrics"]["temporary_role_count"], 1)
        self.assertEqual(payload["metrics"]["organization_admission_count"], 1)
        self.assertEqual(payload["metrics"]["graph_patch_count"], 1)
        self.assertEqual(payload["summary"], "Integrated company result")
        self.assertEqual(len(server.captures), 3)
        final_messages = str(server.captures[2]["body"]["messages"])
        self.assertIn("Repository evidence", final_messages)
        self.assertIn("Security evidence", final_messages)

    def test_company_goal_skips_preflight_compiler_entirely(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, contract_server(
            {"json": completion_body(summary="Solo-first completed safely")},
        ) as (server, base_url):
            exit_code = main(
                [
                    "run",
                    "Inspect safely",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(Path(temporary) / "runtime.db"),
                    "--base-url",
                    base_url,
                    "--model",
                    "contract-model",
                    "--no-auth",
                    "--json",
                ],
                stdout=output,
                stderr=error,
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["planning_mode"], "SOLO")
        self.assertEqual(payload["planning_reason"], "SOLO_FIRST_ATTEMPT")
        self.assertEqual(payload["compiler_usage"]["model_calls"], 0)
        self.assertEqual(len(payload["final_tasks"]), 1)
        self.assertEqual(len(server.captures), 1)
        self.assertEqual(
            server.captures[0]["body"]["response_format"]["json_schema"]["name"],
            "dynamic_firm_employee_completion",
        )

    def test_large_repository_persists_bounded_v2_identity_without_raw_paths(self) -> None:
        provider = ScriptedModelProvider(
            [ModelResponse(completion=CompletionEnvelope(summary="Large repository inspected."))]
        )
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "operator-private-workspace"
            workspace.mkdir()
            (workspace / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            for index in range(501):
                (workspace / f"private-file-{index:03d}.py").touch()
            state = root / "runtime.db"
            exit_code = main(
                [
                    "run",
                    "Inspect the large repository",
                    "--workspace",
                    str(workspace),
                    "--state",
                    str(state),
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "contract-model",
                    "--no-auth",
                    "--json",
                ],
                provider_factory=lambda config: provider,
                stdout=output,
                stderr=error,
            )
            store = RunStore(state)
            try:
                snapshot = json.loads(store.list_job_snapshot_rows(1)[0]["payload_json"])
            finally:
                store.close()
            database_bytes = state.read_bytes()

        identity = snapshot["workspace_identity"]
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(identity["status"], "READY")
        self.assertTrue(identity["context_fingerprint"].startswith("wctx2-"))
        self.assertEqual(identity["failure_code"], "")
        self.assertEqual(len(provider.requests), 1)
        self.assertNotIn(b"private-file-500.py", database_bytes)
        self.assertNotIn(b"operator-private-workspace", database_bytes)

    def test_workspace_identity_failure_is_redacted_in_ledger_and_disables_learning(self) -> None:
        provider = ScriptedModelProvider(
            [ModelResponse(completion=CompletionEnvelope(summary="Fail-closed result."))]
        )
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "runtime.db"
            with patch(
                "dynamic_firm.cli.project_workspace_structure",
                side_effect=WorkspaceProjectionError(
                    WorkspaceProjectionFailureCode.ROOT_UNREADABLE
                ),
            ):
                exit_code = main(
                    [
                        "run",
                        "Inspect without identity",
                        "--workspace",
                        str(FIXTURE_ROOT),
                        "--state",
                        str(state),
                        "--base-url",
                        "http://127.0.0.1:9/v1",
                        "--model",
                        "contract-model",
                        "--no-auth",
                        "--json",
                    ],
                    provider_factory=lambda config: provider,
                    stdout=output,
                    stderr=error,
                )
            store = RunStore(state)
            try:
                snapshot = json.loads(store.list_job_snapshot_rows(1)[0]["payload_json"])
            finally:
                store.close()
            with CompanyStateStore(state) as company:
                summary = company.summary()

        identity = snapshot["workspace_identity"]
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(identity["status"], "FAILED")
        self.assertEqual(identity["context_fingerprint"], "")
        self.assertEqual(identity["failure_code"], "ROOT_UNREADABLE")
        self.assertEqual(summary.episode_count, 0)
        self.assertEqual(len(provider.requests), 1)

    def test_one_model_call_budget_skips_compiler(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, contract_server(
            {"json": completion_body(summary="One employee call completed")},
        ) as (server, base_url):
            exit_code = main(
                [
                    "run",
                    "Inspect with one call",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(Path(temporary) / "runtime.db"),
                    "--base-url",
                    base_url,
                    "--model",
                    "contract-model",
                    "--no-auth",
                    "--max-model-calls",
                    "1",
                    "--json",
                ],
                stdout=output,
                stderr=error,
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["planning_reason"], "SOLO_FIRST_ATTEMPT")
        self.assertEqual(payload["metrics"]["usage"]["model_calls"], 1)
        self.assertEqual(len(server.captures), 1)
        self.assertEqual(
            server.captures[0]["body"]["response_format"]["json_schema"]["name"],
            "dynamic_firm_employee_completion",
        )

    def test_missing_model_configuration_is_an_input_error(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        exit_code = main(
            [
                "run",
                "Inspect",
                "--workspace",
                str(FIXTURE_ROOT),
                "--base-url",
                "http://127.0.0.1:9/v1",
                "--model",
                "",
            ],
            stdout=output,
            stderr=error,
        )
        self.assertEqual(exit_code, EXIT_INPUT)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Model identifier is required", error.getvalue())

    def test_demo_runs_offline_dynamic_fixture_without_provider_configuration(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        exit_code = main(
            ["demo", "replan", "--strategy", "dynamic", "--json"],
            stdout=output,
            stderr=error,
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["fixture"], "replan")
        self.assertEqual(payload["strategy"], "dynamic")
        self.assertEqual(payload["quality_score"], 1.0)
        self.assertEqual(payload["temporary_role_count"], 1)
        self.assertEqual(payload["graph_mutations"], 1)

    def test_doctor_uses_non_secret_toml_and_never_prints_credential(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        secret = "doctor-secret-must-not-print"
        previous = os.environ.get("DOCTOR_MODEL_KEY")
        os.environ["DOCTOR_MODEL_KEY"] = secret
        try:
            with tempfile.TemporaryDirectory() as temporary:
                config_path = Path(temporary) / "config.toml"
                config_path.write_text(
                    "[provider]\n"
                    'base_url = "https://example.invalid/v1"\n'
                    'model = "contract-model"\n'
                    'api_key_env = "DOCTOR_MODEL_KEY"\n',
                    encoding="utf-8",
                )
                exit_code = main(
                    ["--config", str(config_path), "doctor", "--json"],
                    stdout=output,
                    stderr=error,
                )
        finally:
            if previous is None:
                os.environ.pop("DOCTOR_MODEL_KEY", None)
            else:
                os.environ["DOCTOR_MODEL_KEY"] = previous

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertTrue(payload["run_ready"])
        self.assertEqual(payload["provider"]["api_key_env"], "DOCTOR_MODEL_KEY")
        self.assertEqual(payload["external_read"], {"enabled": False})
        self.assertFalse(payload["outbound_channel"]["enabled"])
        self.assertEqual(
            payload["execution_environment"]["remote_job_execution"],
            "DISABLED_UNTIL_EXPLICIT_OPERATOR_CONFIGURATION",
        )
        self.assertFalse(payload["execution_environment"]["remote_worker"]["enabled"])
        self.assertFalse(payload["release_installation"]["network_accessed"])
        self.assertFalse(payload["release_installation"]["local_state_touched"])
        self.assertEqual(
            payload["terminal_diagnostics"]["contents"],
            "redacted_exception_type_and_stack_locations_only",
        )
        self.assertTrue(payload["terminal_diagnostics"]["crash_log_path"].endswith("modern-terminal-crashes.log"))
        self.assertEqual(payload["employee_runtime"]["required_distribution"], "PyYAML==6.0.3")
        self.assertEqual(payload["employee_runtime"]["resolved_runtime"], "noruct")
        self.assertTrue(payload["employee_runtime"]["worker_python"])
        self.assertNotIn(secret, output.getvalue())

    def test_doctor_projects_explicit_mcp_multi_tool_contract_without_connecting_server(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            python = str(Path(os.sys.executable).resolve())
            config_path.write_text(
                "[provider]\n"
                'base_url = "https://example.invalid/v1"\n'
                'model = "contract-model"\n'
                "\n[mcp]\n"
                "enabled = true\n"
                f'python_command = "{python}"\n'
                f'server_command = "{python}"\n'
                'tool_names = ["read_issue", "second_tool"]\n',
                encoding="utf-8",
            )
            exit_code = main(
                ["--config", str(config_path), "doctor", "--json"],
                stdout=output,
                stderr=error,
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_INPUT, error.getvalue())
        self.assertTrue(payload["external_read"]["enabled"])
        self.assertEqual(
            payload["external_read"]["public_tools"],
            [
                "read_external_external_context_1_c3e576bf16dd",
                "read_external_external_context_2_ca8200cfc4e9",
            ],
        )
        self.assertEqual(
            payload["external_read"]["authority"],
            "explicit_read_only_allowlist_one_call_per_selected_tool_per_job",
        )
        self.assertNotIn("read_issue", str(payload["external_read"]))

    def test_mcp_configure_status_and_disable_preserve_the_non_secret_config_boundary(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            executable = str(Path(sys.executable).resolve())
            configured = main(
                [
                    "--config", str(config_path), "mcp", "configure",
                    "--python-command", executable,
                    "--server-command", executable,
                    "--server-arg", "fixture",
                    "--tool", "read_issue",
                    "--json",
                ],
                stdout=output,
                stderr=error,
            )
            payload = json.loads(output.getvalue())
            self.assertIn(configured, {EXIT_OK, EXIT_INPUT})
            self.assertTrue(payload["configuration_changed"])
            self.assertEqual(payload["tool_count"], 1)
            self.assertNotIn("read_issue", str(payload["runtime_tools"]))
            self.assertNotIn("read_issue", str(payload))

            output.seek(0); output.truncate(0)
            disabled = main(
                ["--config", str(config_path), "mcp", "disable", "--json"],
                stdout=output,
                stderr=error,
            )
            disabled_payload = json.loads(output.getvalue())
            self.assertEqual(disabled, EXIT_OK, error.getvalue())
            self.assertTrue(disabled_payload["configuration_changed"])
            self.assertFalse(disabled_payload["enabled"])

    def test_mcp_action_profile_is_separate_and_never_exposes_upstream_tool_name(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            executable = str(Path(sys.executable).resolve())
            configured = main(
                [
                    "--config", str(config_path), "mcp", "action-configure",
                    "--python-command", executable,
                    "--server-command", executable,
                    "--server-arg", "fixture",
                    "--tool", "write_issue",
                    "--profile", "issue-action",
                    "--json",
                ],
                stdout=output,
                stderr=error,
            )
            payload = json.loads(output.getvalue())
            self.assertIn(configured, {EXIT_OK, EXIT_INPUT})
            self.assertTrue(payload["configuration_changed"])
            self.assertEqual(payload["public_tools"], ["run_external_action"])
            self.assertNotIn("write_issue", str(payload))

            output.seek(0)
            output.truncate(0)
            disabled = main(
                ["--config", str(config_path), "mcp", "action-disable", "--json"],
                stdout=output,
                stderr=error,
            )
            disabled_payload = json.loads(output.getvalue())

        self.assertEqual(disabled, EXIT_OK, error.getvalue())
        self.assertTrue(disabled_payload["configuration_changed"])
        self.assertFalse(disabled_payload["enabled"])

    def test_mcp_action_profiles_add_and_remove_with_private_runtime_names(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            executable = str(Path(sys.executable).resolve())
            first = main(
                [
                    "--config", str(config_path), "mcp", "action-configure",
                    "--python-command", executable, "--server-command", executable,
                    "--tool", "write_issue", "--profile", "issues", "--json",
                ], stdout=output, stderr=error,
            )
            self.assertIn(first, {EXIT_OK, EXIT_INPUT}, error.getvalue())
            output.seek(0); output.truncate(0)
            second = main(
                [
                    "--config", str(config_path), "mcp", "action-add",
                    "--python-command", executable, "--server-command", executable,
                    "--tool", "send_notice", "--profile", "notices", "--json",
                ], stdout=output, stderr=error,
            )
            self.assertIn(second, {EXIT_OK, EXIT_INPUT}, error.getvalue())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["profile_count"], 2)
            self.assertEqual(len(payload["public_tools"]), 2)
            self.assertTrue(all(name.startswith("run_external_action_") for name in payload["public_tools"]))
            self.assertNotIn("send_notice", str(payload))
            output.seek(0); output.truncate(0)
            removed = main(
                ["--config", str(config_path), "mcp", "action-remove", "--profile", "issues", "--json"],
                stdout=output, stderr=error,
            )
            self.assertIn(removed, {EXIT_OK, EXIT_INPUT}, error.getvalue())
            collapsed = json.loads(output.getvalue())
            self.assertEqual(collapsed["profile_count"], 1)
            self.assertEqual(collapsed["public_tools"], ["run_external_action"])

    def test_browser_subcommands_are_not_rewritten_as_a_conversational_goal(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            configured = main(
                [
                    "--config", str(config_path), "browser", "configure",
                    "--node-command", str(Path(sys.executable).resolve()),
                    "--cdp-endpoint", "http://127.0.0.1:9222", "--json",
                ],
                stdout=output,
                stderr=error,
            )
            payload = json.loads(output.getvalue())
            output.seek(0); output.truncate(0)
            disabled = main(
                ["--config", str(config_path), "browser", "disable", "--json"],
                stdout=output,
                stderr=error,
            )
            disabled_payload = json.loads(output.getvalue())

        self.assertEqual(configured, EXIT_INPUT, error.getvalue())
        self.assertTrue(payload["configuration_changed"])
        self.assertTrue(payload["enabled"])
        self.assertEqual(disabled, EXIT_OK, error.getvalue())
        self.assertTrue(disabled_payload["configuration_changed"])
        self.assertFalse(disabled_payload["enabled"])

    def test_doctor_projects_optional_browser_read_policy_without_connecting_to_browser(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                "[browser]\n"
                "enabled = true\n"
                f'node_command = "{Path(sys.executable).resolve()}"\n'
                'cdp_endpoint = "http://127.0.0.1:9222"\n',
                encoding="utf-8",
            )
            exit_code = main(
                ["--config", str(config_path), "doctor", "--json"],
                stdout=output,
                stderr=error,
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_INPUT, error.getvalue())
        self.assertTrue(payload["local_browser_read"]["enabled"])
        self.assertEqual(payload["local_browser_read"]["endpoint"], "configured_loopback")
        self.assertNotIn("9222", json.dumps(payload["local_browser_read"]))

    def test_mcp_policy_package_is_local_catalog_only_and_never_starts_a_server(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.toml"
            state_path = root / "runtime.db"
            executable = str(Path(sys.executable).resolve())
            self.assertIn(
                main(
                    [
                        "--config", str(config_path), "mcp", "configure",
                        "--python-command", executable, "--server-command", executable,
                        "--tool", "read_issue", "--json",
                    ],
                    stdout=output,
                    stderr=error,
                ),
                {EXIT_OK, EXIT_INPUT},
            )
            output.seek(0); output.truncate(0)
            preview = main(
                [
                    "--config", str(config_path), "mcp", "package", "preview",
                    "--artifact-id", "repository_mcp_policy", "--version", "1.0.0",
                    "--state", str(state_path), "--json",
                ],
                stdout=output,
                stderr=error,
            )
            preview_payload = json.loads(output.getvalue())
            self.assertEqual(preview, EXIT_OK, error.getvalue())
            self.assertEqual(preview_payload["artifact"]["kind"], "TOOL_PACKAGE")
            self.assertEqual(preview_payload["artifact"]["release_channel"], "EXPERIMENTAL")
            self.assertNotIn("read_issue", str(preview_payload))
            output.seek(0); output.truncate(0)
            rejected = main(
                [
                    "--config", str(config_path), "mcp", "package", "register",
                    "--artifact-id", "repository_mcp_policy", "--version", "1.0.0",
                    "--state", str(state_path), "--json",
                ],
                stdout=output,
                stderr=error,
            )
            self.assertEqual(rejected, EXIT_INPUT)
            output.seek(0); output.truncate(0)
            registered = main(
                [
                    "--config", str(config_path), "mcp", "package", "register",
                    "--artifact-id", "repository_mcp_policy", "--version", "1.0.0",
                    "--state", str(state_path), "--confirm", "--json",
                ],
                stdout=output,
                stderr=error,
            )
            payload = json.loads(output.getvalue())
            output.seek(0); output.truncate(0)
            listed = main(
                [
                    "--config", str(config_path), "mcp", "package", "list",
                    "--state", str(state_path), "--json",
                ],
                stdout=output,
                stderr=error,
            )
            listed_payload = json.loads(output.getvalue())
            self.assertEqual(listed, EXIT_OK, error.getvalue())
            self.assertEqual(listed_payload["package_count"], 1)
            self.assertEqual(listed_payload["packages"][0]["artifact_id"], "repository_mcp_policy")
            self.assertNotIn("read_issue", str(listed_payload))
            output.seek(0); output.truncate(0)
            staged = main(
                [
                    "--config", str(config_path), "evolution", "artifact", "stage",
                    "repository_mcp_policy", "1.0.0", "--state", str(state_path), "--confirm", "--json",
                ],
                stdout=output,
                stderr=error,
            )
            self.assertEqual(staged, EXIT_OK, error.getvalue())
            output.seek(0); output.truncate(0)
            installed = main(
                [
                    "--config", str(config_path), "evolution", "artifact", "install",
                    "repository_mcp_policy", "1.0.0", "--state", str(state_path), "--confirm", "--json",
                ],
                stdout=output,
                stderr=error,
            )
            self.assertEqual(installed, EXIT_OK, error.getvalue())
            output.seek(0); output.truncate(0)
            activated = main(
                [
                    "--config", str(config_path), "evolution", "artifact", "activate",
                    "company_default", "repository_mcp_policy", "1.0.0",
                    "--allowed-capability", "external_read", "--state", str(state_path), "--confirm", "--json",
                ],
                stdout=output,
                stderr=error,
            )
            activation = json.loads(output.getvalue())
            output.seek(0); output.truncate(0)
            package_status = main(
                [
                    "--config", str(config_path), "mcp", "package", "status",
                    "--scope", "company_default", "--state", str(state_path), "--json",
                ],
                stdout=output,
                stderr=error,
            )
            package_status_payload = json.loads(output.getvalue())
            self.assertEqual(package_status, EXIT_OK, error.getvalue())
            self.assertEqual(package_status_payload["packages"][0]["binding_status"], "MATCHES_CONFIGURED_POLICY")
            output.seek(0); output.truncate(0)
            drifted_config = main(
                [
                    "--config", str(config_path), "mcp", "configure",
                    "--python-command", executable, "--server-command", executable,
                    "--tool", "different_read_tool", "--profile", "drifted-context", "--json",
                ],
                stdout=output,
                stderr=error,
            )
            self.assertIn(drifted_config, {EXIT_OK, EXIT_INPUT}, error.getvalue())
            output.seek(0); output.truncate(0)
            drifted_status = main(
                [
                    "--config", str(config_path), "mcp", "package", "status",
                    "--scope", "company_default", "--state", str(state_path), "--json",
                ],
                stdout=output,
                stderr=error,
            )
            drifted_payload = json.loads(output.getvalue())
            self.assertEqual(drifted_status, EXIT_INPUT, error.getvalue())
            self.assertEqual(drifted_payload["packages"][0]["binding_status"], "DRIFTED_FROM_CONFIGURED_POLICY")
        self.assertEqual(registered, EXIT_OK, error.getvalue())
        self.assertEqual(payload["artifact"]["kind"], "TOOL_PACKAGE")
        self.assertEqual(payload["artifact"]["release_channel"], "EXPERIMENTAL")
        self.assertEqual(activated, EXIT_OK, error.getvalue())
        self.assertEqual(activation["status"], "ACTIVE")

    def test_mcp_policy_package_can_be_registered_for_one_configured_profile(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.toml"
            state_path = root / "runtime.db"
            executable = str(Path(sys.executable).resolve())
            self.assertIn(
                main(
                    [
                        "--config", str(config_path), "mcp", "configure",
                        "--python-command", executable, "--server-command", executable,
                        "--tool", "read_repository", "--profile", "repository-context", "--json",
                    ],
                    stdout=output,
                    stderr=error,
                ),
                {EXIT_OK, EXIT_INPUT},
            )
            output.seek(0); output.truncate(0)
            self.assertIn(
                main(
                    [
                        "--config", str(config_path), "mcp", "add",
                        "--python-command", executable, "--server-command", executable,
                        "--server-arg", "issues", "--tool", "read_issue", "--profile", "issue-context", "--json",
                    ],
                    stdout=output,
                    stderr=error,
                ),
                {EXIT_OK, EXIT_INPUT},
            )
            output.seek(0); output.truncate(0)
            preview = main(
                [
                    "--config", str(config_path), "mcp", "package", "preview",
                    "--artifact-id", "repository_mcp_policy_only", "--version", "1.0.0",
                    "--profile", "repository-context", "--state", str(state_path), "--json",
                ],
                stdout=output,
                stderr=error,
            )
            payload = json.loads(output.getvalue())
        self.assertEqual(preview, EXIT_OK, error.getvalue())
        self.assertEqual(payload["profile"], "repository-context")
        self.assertNotIn("read_repository", str(payload))
        self.assertNotIn("issue-context", str(payload))

    def test_mcp_add_and_remove_preserve_independent_profile_boundaries(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            executable = str(Path(sys.executable).resolve())
            self.assertIn(
                main(
                    [
                        "--config", str(config_path), "mcp", "configure",
                        "--python-command", executable, "--server-command", executable,
                        "--tool", "read_issue", "--profile", "repository-context", "--json",
                    ], stdout=output, stderr=error,
                ),
                {EXIT_OK, EXIT_INPUT},
            )
            output.seek(0); output.truncate(0)
            self.assertIn(
                main(
                    [
                        "--config", str(config_path), "mcp", "add",
                        "--python-command", executable, "--server-command", executable,
                        "--tool", "read_issue", "--profile", "issue-context", "--json",
                    ], stdout=output, stderr=error,
                ),
                {EXIT_OK, EXIT_INPUT},
            )
            added = json.loads(output.getvalue())
            self.assertEqual(added["profile_count"], 2)
            self.assertEqual({item["profile"] for item in added["profiles"]}, {"repository-context", "issue-context"})
            self.assertNotIn("read_issue", str(added))
            output.seek(0); output.truncate(0)
            removed = main(
                ["--config", str(config_path), "mcp", "remove", "issue-context", "--json"],
                stdout=output, stderr=error,
            )
            payload = json.loads(output.getvalue())
        self.assertIn(removed, {EXIT_OK, EXIT_INPUT}, error.getvalue())
        self.assertTrue(payload["configuration_changed"])
        self.assertEqual(payload["profile_count"], 1)
        self.assertEqual(payload["profile"], "repository-context")

    def test_environment_status_is_a_local_read_only_product_surface(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            code = main(
                ["environment", "status", "--workspace", temporary, "--json"],
                stdout=output,
                stderr=error,
            )
        self.assertEqual(code, EXIT_OK, error.getvalue())
        record = json.loads(output.getvalue())
        self.assertEqual(
            record["remote_job_execution"],
            "DISABLED_UNTIL_EXPLICIT_OPERATOR_CONFIGURATION",
        )
        self.assertFalse(record["remote_worker"]["enabled"])
        self.assertEqual(record["local_execution"], "AVAILABLE_PER_ACTION_APPROVAL")

    def test_remote_worker_configure_status_and_disable_are_receipt_bound_and_non_secret(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.toml"
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps({
                "host": "build.example.test", "user": "operator", "port": 22,
                "remote_snapshot_directory": "/srv/company/.noruct-remote-snapshots/" + "a" * 64,
                "snapshot_sha256": "a" * 64, "transferred": True,
                "integrity_state": "VERIFIED_REMOTE_SNAPSHOT", "host_key_policy": "STRICT_KNOWN_HOSTS_ONLY",
                "remote_job_execution": "NOT_IMPLEMENTED",
            }), encoding="utf-8")
            configured = main([
                "--config", str(config_path), "environment", "worker-configure",
                "--target-id", "build", "--receipt", str(receipt),
                "--program", "tests=/usr/bin/pytest", "--json",
            ], stdout=output, stderr=error)
            payload = json.loads(output.getvalue())
            self.assertEqual(configured, EXIT_OK, error.getvalue())
            self.assertTrue(payload["enabled"])
            self.assertEqual(payload["program_ids"], ["tests"])
            self.assertEqual(payload["permission_mode_required"], "ask")
            self.assertNotIn("identity_file", config_path.read_text(encoding="utf-8"))
            output.seek(0); output.truncate(0)
            status = main(["--config", str(config_path), "environment", "worker-status", "--json"], stdout=output, stderr=error)
            self.assertEqual(status, EXIT_OK, error.getvalue())
            self.assertTrue(json.loads(output.getvalue())["ready"])
            output.seek(0); output.truncate(0)
            with patch(
                "dynamic_firm.cli.verify_remote_workspace_worker",
                return_value=SimpleNamespace(to_dict=lambda: {
                    "reachable": True, "snapshot_present": True,
                    "host": "build.example.test", "port": 22,
                    "authority": "fixed_marker_only",
                }),
            ) as verify:
                verified = main(["--config", str(config_path), "environment", "worker-verify", "--confirm", "--json"], stdout=output, stderr=error)
            self.assertEqual(verified, EXIT_OK, error.getvalue())
            self.assertTrue(json.loads(output.getvalue())["snapshot_present"])
            verify.assert_called_once()
            output.seek(0); output.truncate(0)
            with patch(
                "dynamic_firm.cli.verify_remote_workspace_worker_content",
                return_value=SimpleNamespace(to_dict=lambda: {
                    "reachable": True, "snapshot_present": True, "content_verified": True,
                    "integrity_state": "VERIFIED_REMOTE_LEDGER", "host": "build.example.test", "port": 22,
                }),
            ) as audit:
                audited = main(["--config", str(config_path), "environment", "worker-audit", "--confirm", "--json"], stdout=output, stderr=error)
            self.assertEqual(audited, EXIT_OK, error.getvalue())
            self.assertTrue(json.loads(output.getvalue())["content_verified"])
            audit.assert_called_once()
            output.seek(0); output.truncate(0)
            disabled = main(["--config", str(config_path), "environment", "worker-disable", "--json"], stdout=output, stderr=error)
        self.assertEqual(disabled, EXIT_OK, error.getvalue())
        self.assertFalse(json.loads(output.getvalue())["enabled"])

    def test_container_workspace_configure_status_and_disable_never_start_docker(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            configured = main([
                "--config", str(config_path), "environment", "container-configure",
                "--image", "python:3.11-alpine", "--program", "tests=/usr/bin/pytest", "--json",
            ], stdout=output, stderr=error)
            payload = json.loads(output.getvalue())
            self.assertEqual(configured, EXIT_OK, error.getvalue())
            self.assertTrue(payload["enabled"])
            self.assertEqual(payload["network"], "disabled")
            self.assertFalse(payload["automatic_activation"])
            self.assertNotIn("docker run", config_path.read_text(encoding="utf-8"))
            output.seek(0); output.truncate(0)
            with patch(
                "dynamic_firm.cli.verify_container_workspace",
                return_value=SimpleNamespace(to_dict=lambda: {
                    "runtime_available": True, "image_present": True,
                    "image_reference_pinned": False, "image": "python:3.11-alpine",
                }),
            ) as verify:
                verified = main(
                    ["--config", str(config_path), "environment", "container-verify", "--confirm", "--json"],
                    stdout=output, stderr=error,
                )
            self.assertEqual(verified, EXIT_OK, error.getvalue())
            self.assertTrue(json.loads(output.getvalue())["image_present"])
            verify.assert_called_once()
            output.seek(0); output.truncate(0)
            status = main(["--config", str(config_path), "environment", "container-status", "--json"], stdout=output, stderr=error)
            self.assertEqual(status, EXIT_OK, error.getvalue())
            self.assertEqual(json.loads(output.getvalue())["program_ids"], ["tests"])
            output.seek(0); output.truncate(0)
            disabled = main(["--config", str(config_path), "environment", "container-disable", "--json"], stdout=output, stderr=error)
        self.assertEqual(disabled, EXIT_OK, error.getvalue())
        self.assertFalse(json.loads(output.getvalue())["enabled"])

    def test_execution_environment_preflight_requires_confirmation_and_combines_configured_boundaries(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.toml"
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps({
                "host": "build.example.test", "user": "operator", "port": 22,
                "remote_snapshot_directory": "/srv/company/.noruct-remote-snapshots/" + "a" * 64,
                "snapshot_sha256": "a" * 64, "transferred": True,
                "integrity_state": "VERIFIED_REMOTE_SNAPSHOT", "host_key_policy": "STRICT_KNOWN_HOSTS_ONLY",
                "remote_job_execution": "NOT_IMPLEMENTED",
            }), encoding="utf-8")
            self.assertEqual(main([
                "--config", str(config_path), "environment", "worker-configure", "--target-id", "build",
                "--receipt", str(receipt), "--program", "tests=/usr/bin/pytest", "--json",
            ], stdout=io.StringIO(), stderr=error), EXIT_OK, error.getvalue())
            self.assertEqual(main([
                "--config", str(config_path), "environment", "container-configure", "--image", "python:3.11-alpine",
                "--program", "tests=/usr/bin/pytest", "--json",
            ], stdout=io.StringIO(), stderr=error), EXIT_OK, error.getvalue())
            denied = main(["--config", str(config_path), "environment", "preflight", "--json"], stdout=io.StringIO(), stderr=io.StringIO())
            with patch(
                "dynamic_firm.cli.verify_remote_workspace_worker_content",
                return_value=SimpleNamespace(to_dict=lambda: {"content_verified": True, "integrity_state": "VERIFIED_REMOTE_LEDGER"}),
            ) as remote_audit, patch(
                "dynamic_firm.cli.verify_container_workspace",
                return_value=SimpleNamespace(to_dict=lambda: {"runtime_available": True, "image_present": True}),
            ) as container_audit:
                code = main(
                    ["--config", str(config_path), "environment", "preflight", "--confirm", "--json"],
                    stdout=output, stderr=error,
                )
        payload = json.loads(output.getvalue())
        self.assertNotEqual(denied, EXIT_OK)
        self.assertEqual(code, EXIT_OK, error.getvalue())
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["configured_count"], 2)
        self.assertTrue(payload["remote_worker"]["content_verified"])
        self.assertTrue(payload["container_workspace"]["image_present"])
        remote_audit.assert_called_once()
        container_audit.assert_called_once()

    def test_execution_environment_preflight_fails_closed_when_no_execution_boundary_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            code = main(
                ["--config", str(Path(temporary) / "config.toml"), "environment", "preflight", "--confirm", "--json"],
                stdout=output, stderr=io.StringIO(),
            )
        payload = json.loads(output.getvalue())
        self.assertNotEqual(code, EXIT_OK)
        self.assertEqual(payload["configured_count"], 0)
        self.assertFalse(payload["ready"])

    def test_company_curate_daemon_is_foreground_candidate_only_and_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "company.db"
            denied = main(["company", "curate-daemon", "--state", str(state), "--max-cycles", "1", "--json"], stdout=io.StringIO(), stderr=io.StringIO())
            output = io.StringIO()
            code = main([
                "company", "curate-daemon", "--state", str(state), "--max-cycles", "1", "--confirm", "--json",
            ], stdout=output, stderr=io.StringIO())
        payload = json.loads(output.getvalue())
        self.assertNotEqual(denied, EXIT_OK)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["curator"], "foreground_operator_confirmed_deterministic_loop")
        self.assertEqual(len(payload["cycles"]), 1)
        self.assertFalse(payload["automatic_approve"])
        self.assertFalse(payload["automatic_apply"])
        self.assertEqual(payload["provider_calls"], 0)
        self.assertEqual(payload["company_jobs_created"], 0)

    def test_doctor_reports_configured_remote_and_container_boundaries_without_claiming_readiness(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.toml"
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps({
                "host": "build.example.test", "user": "operator", "port": 22,
                "remote_snapshot_directory": "/srv/company/.noruct-remote-snapshots/" + "a" * 64,
                "snapshot_sha256": "a" * 64, "transferred": True,
                "integrity_state": "VERIFIED_REMOTE_SNAPSHOT", "host_key_policy": "STRICT_KNOWN_HOSTS_ONLY",
                "remote_job_execution": "NOT_IMPLEMENTED",
            }), encoding="utf-8")
            self.assertEqual(main([
                "--config", str(config_path), "environment", "worker-configure", "--target-id", "build",
                "--receipt", str(receipt), "--program", "tests=/usr/bin/pytest", "--json",
            ], stdout=io.StringIO(), stderr=error), EXIT_OK, error.getvalue())
            self.assertEqual(main([
                "--config", str(config_path), "environment", "container-configure", "--image", "python:3.11-alpine",
                "--program", "tests=/usr/bin/pytest", "--json",
            ], stdout=io.StringIO(), stderr=error), EXIT_OK, error.getvalue())
            code = main(["--config", str(config_path), "doctor", "--json"], stdout=output, stderr=error)
        payload = json.loads(output.getvalue())
        self.assertNotEqual(code, EXIT_OK)  # Provider setup remains independent of optional execution paths.
        execution = payload["execution_environment"]
        self.assertTrue(execution["remote_worker"]["enabled"])
        self.assertTrue(execution["container_workspace"]["enabled"])
        self.assertNotIn("reachable", execution["remote_worker"])
        self.assertNotIn("image_present", execution["container_workspace"])

    def test_channel_configure_status_and_disable_are_non_secret_and_not_automatic(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            configured = main(
                [
                    "--config", str(config_path), "channel", "configure",
                    "--command", str(Path(sys.executable).resolve()),
                    "--arg=-c", "--arg", "import sys; sys.stdin.read()", "--json",
                ],
                stdout=output,
                stderr=error,
            )
            payload = json.loads(output.getvalue())
            self.assertEqual(configured, EXIT_OK, error.getvalue())
            self.assertTrue(payload["enabled"])
            self.assertFalse(payload["automatic_delivery"])
            output.seek(0); output.truncate(0)
            disabled = main(
                ["--config", str(config_path), "channel", "disable", "--json"],
                stdout=output,
                stderr=error,
            )
        self.assertEqual(disabled, EXIT_OK, error.getvalue())
        self.assertFalse(json.loads(output.getvalue())["enabled"])

    def test_mcp_test_requires_operator_confirmation_before_starting_the_sidecar(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            executable = str(Path(sys.executable).resolve())
            config_path.write_text(
                "[mcp]\n"
                "enabled = true\n"
                f'python_command = "{executable}"\n'
                f'server_command = "{executable}"\n'
                'tool_names = ["read_issue"]\n',
                encoding="utf-8",
            )
            exit_code = main(
                [
                    "--config", str(config_path), "mcp", "test",
                    "--tool-index", "1", "--arguments-json", "{}",
                ],
                stdout=output,
                stderr=error,
            )
        self.assertEqual(exit_code, EXIT_INPUT)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("requires --confirm", error.getvalue())

    def test_update_status_is_local_only_and_activation_requires_confirmation(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "releases"
            bin_dir = Path(temporary) / "bin"
            exit_code = main(
                [
                    "update", "status", "--install-root", str(root),
                    "--bin-dir", str(bin_dir), "--json",
                ],
                stdout=output,
                stderr=error,
            )
            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, EXIT_OK, error.getvalue())
            self.assertFalse(payload["network_accessed"])
            self.assertFalse(payload["local_state_touched"])
            output.seek(0); output.truncate(0)
            denied = main(
                [
                    "update", "activate", "0.0.80", "--install-root", str(root),
                    "--bin-dir", str(bin_dir),
                ],
                stdout=output,
                stderr=error,
            )
        self.assertEqual(denied, EXIT_INPUT)
        self.assertIn("requires --confirm", error.getvalue())

    def test_remote_operator_command_requires_confirmation(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        exit_code = main(
            [
                "environment", "ssh-command", "--host", "build.example.test",
                "--user", "operator", "--remote-workspace", "/srv/workspace",
                "--program", "/usr/bin/true",
            ],
            stdout=output,
            stderr=error,
        )
        self.assertEqual(exit_code, EXIT_INPUT)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("requires --confirm", error.getvalue())

    def test_terminal_job_channel_delivery_requires_confirmation_before_reading_state(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        exit_code = main(
            ["channel", "job-summary", "job-unknown", "--state", "/missing/runtime.db"],
            stdout=output,
            stderr=error,
        )
        self.assertEqual(exit_code, EXIT_INPUT)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("requires --confirm", error.getvalue())

    def test_workspace_snapshot_requires_confirmation_and_writes_no_remote_job(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            (workspace / "note.txt").write_text("safe", encoding="utf-8")
            manifest = Path(temporary) / "manifest.json"
            denied = main(
                ["environment", "snapshot", "--workspace", str(workspace), "--output", str(manifest)],
                stdout=output,
                stderr=error,
            )
            self.assertEqual(denied, EXIT_INPUT)
            self.assertFalse(manifest.exists())
            self.assertIn("requires --confirm", error.getvalue())
            output.seek(0); output.truncate(0); error.seek(0); error.truncate(0)
            completed = main(
                [
                    "environment", "snapshot", "--workspace", str(workspace), "--output", str(manifest),
                    "--confirm", "--json",
                ],
                stdout=output,
                stderr=error,
            )
            payload = json.loads(output.getvalue())
        self.assertEqual(completed, EXIT_OK, error.getvalue())
        self.assertEqual(payload["remote_job_execution"], "NOT_IMPLEMENTED")
        self.assertEqual(payload["file_count"], 1)

    def test_remote_workspace_transfer_requires_confirmation_before_reading_or_uploading_snapshot(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        exit_code = main(
            [
                "environment", "ssh-transfer", "--workspace", "/missing/workspace", "--snapshot", "/missing/manifest.json",
                "--host", "build.example.test", "--user", "operator", "--remote-workspace", "/srv/workspace",
            ],
            stdout=output,
            stderr=error,
        )
        self.assertEqual(exit_code, EXIT_INPUT)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("requires --confirm", error.getvalue())

    def test_workspace_snapshot_inspect_is_read_only_and_reports_invalid_manifest(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "invalid-snapshot.json"
            manifest.write_text("{}", encoding="utf-8")
            exit_code = main(
                ["environment", "snapshot-inspect", "--source", str(manifest), "--json"],
                stdout=output,
                stderr=error,
            )
            payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertFalse(payload["valid"])
        self.assertEqual(payload["remote_job_execution"], "NOT_IMPLEMENTED")

    def test_provider_status_is_non_network_and_preflight_requires_confirmation(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                "[provider]\n"
                'kind = "ollama"\n'
                'base_url = "http://127.0.0.1:11434/v1"\n'
                'model = "local-model"\n'
                "no_auth = true\n",
                encoding="utf-8",
            )
            status = main(
                ["--config", str(config_path), "provider", "status", "--json"],
                stdout=output,
                stderr=error,
            )
            payload = json.loads(output.getvalue())
            self.assertEqual(status, EXIT_OK, error.getvalue())
            self.assertFalse(payload["network_attempted"])
            output.seek(0); output.truncate(0)
            denied = main(
                ["--config", str(config_path), "provider", "preflight"],
                stdout=output,
                stderr=error,
            )
        self.assertEqual(denied, EXIT_INPUT)
        self.assertIn("requires --confirm", error.getvalue())

    def test_external_provider_login_requires_confirmation_and_a_real_terminal(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                "[provider]\n"
                'kind = "openai_codex"\n'
                'codex_command = "codex"\n',
                encoding="utf-8",
            )
            denied = main(
                ["--config", str(config_path), "provider", "login"],
                stdout=output,
                stderr=error,
            )
            self.assertEqual(denied, EXIT_INPUT)
            self.assertIn("requires --confirm", error.getvalue())
            output.seek(0); output.truncate(0); error.seek(0); error.truncate(0)
            noninteractive = main(
                ["--config", str(config_path), "provider", "login", "--confirm"],
                stdout=output,
                stderr=error,
            )
        self.assertEqual(noninteractive, EXIT_INPUT)
        self.assertIn("interactive terminal", error.getvalue())

    def test_config_rejects_secret_value_fields(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                "[provider]\n"
                'base_url = "https://example.invalid/v1"\n'
                'model = "contract-model"\n'
                'api_key = "must-not-live-here"\n',
                encoding="utf-8",
            )
            exit_code = main(
                ["--config", str(config_path), "doctor", "--json"],
                stdout=output,
                stderr=error,
            )

        self.assertEqual(exit_code, EXIT_INPUT)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Secret value field is not allowed", error.getvalue())
        self.assertNotIn("must-not-live-here", error.getvalue())

    def test_company_goal_records_episode_but_defaults_to_no_patch(self) -> None:
        provider = ScriptedModelProvider(
            [ModelResponse(completion=CompletionEnvelope(summary="Inspection completed."))]
        )
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "runtime.db"
            output = io.StringIO()
            error = io.StringIO()
            exit_code = main(
                [
                    "run",
                    "Inspect the repository",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(state),
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "contract-model",
                    "--no-auth",
                ],
                provider_factory=lambda config: provider,
                stdout=output,
                stderr=error,
            )
            status_output = io.StringIO()
            status_code = main(
                [
                    "--config",
                    str(Path(temporary) / "missing.toml"),
                    "company",
                    "status",
                    "--state",
                    str(state),
                    "--json",
                ],
                stdout=status_output,
                stderr=error,
            )
            curate_output = io.StringIO()
            curate_code = main(
                [
                    "--config",
                    str(Path(temporary) / "missing.toml"),
                    "company",
                    "curate",
                    "--state",
                    str(state),
                    "--json",
                ],
                stdout=curate_output,
                stderr=error,
            )
            metrics_output = io.StringIO()
            metrics_code = main(
                [
                    "--config",
                    str(Path(temporary) / "missing.toml"),
                    "company",
                    "organization-metrics",
                    "--state",
                    str(state),
                ],
                stdout=metrics_output,
                stderr=error,
            )

        status = json.loads(status_output.getvalue())
        curation = json.loads(curate_output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(status_code, EXIT_OK, error.getvalue())
        self.assertEqual(curate_code, EXIT_OK, error.getvalue())
        self.assertEqual(metrics_code, EXIT_OK, error.getvalue())
        self.assertEqual(status["summary"]["episode_count"], 1)
        self.assertIn("Graph proposal decisions: approved=0", metrics_output.getvalue())
        self.assertEqual(status["summary"]["workflow_pattern_count"], 0)
        self.assertEqual(curation["decision"], "NO_PATCH")

    def test_company_goal_records_explicit_temporary_staffing_demand(self) -> None:
        provider = ScriptedModelProvider(
            [
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="General review found a specialist boundary.",
                        signals=(
                            RunSignal(
                                SignalCode.CAPABILITY_MISSING,
                                "security_review",
                                ("security review evidence is unavailable",),
                            ),
                        ),
                    )
                ),
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="Temporary security specialist completed the review."
                    )
                ),
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="Integrated security review completed."
                    )
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "runtime.db"
            error = io.StringIO()
            run_code = main(
                [
                    "run",
                    "Review the repository security posture",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(state),
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "contract-model",
                    "--no-auth",
                    "--json",
                ],
                provider_factory=lambda config: provider,
                stdout=io.StringIO(),
                stderr=error,
            )
            demand_output = io.StringIO()
            demand_code = main(
                [
                    "company",
                    "staffing-demands",
                    "--state",
                    str(state),
                    "--json",
                ],
                stdout=demand_output,
                stderr=error,
            )
            recommend_output = io.StringIO()
            recommend_code = main(
                [
                    "company",
                    "roster-recommend",
                    "--state",
                    str(state),
                    "--json",
                ],
                stdout=recommend_output,
                stderr=error,
            )

        demands = json.loads(demand_output.getvalue())
        recommendation = json.loads(recommend_output.getvalue())
        self.assertEqual(run_code, EXIT_OK, error.getvalue())
        self.assertEqual(demand_code, EXIT_OK, error.getvalue())
        self.assertEqual(recommend_code, EXIT_OK, error.getvalue())
        self.assertEqual(len(demands), 1)
        self.assertEqual(demands[0]["capability"], "security_review")
        self.assertIn("no_validation_evidence", demands[0]["safety_violations"])
        self.assertNotIn("temp-", demand_output.getvalue())
        self.assertEqual(recommendation["decision"], "NO_PATCH")
        self.assertEqual(recommendation["active_roster_revision"], 2)

    def test_company_goal_projects_actual_post_hire_assignment(self) -> None:
        context = fixture_workflow_context()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "runtime.db"
            with CompanyStateStore(state) as store:
                store.ensure_roster_baseline(
                    (
                        EmployeeRecord(
                            "employee-generalist",
                            "Generalist",
                            ("conversation",),
                            model_profile="company-default",
                        ),
                    )
                )
                for suffix in ("one", "two"):
                    episode = OrganizationEpisode.create(
                        job_id=f"hire-demand-{suffix}",
                        source=EvidenceSource.REAL_JOB,
                        task_family="staffing.security-review",
                        context_fingerprint=context,
                        execution_profile="READ_ONLY",
                        planning_mode="DYNAMIC",
                        plan_template=(
                            WorkflowTaskTemplate(
                                "security",
                                ("security_review",),
                                final=True,
                            ),
                        ),
                        success=True,
                        quality_score=1.0,
                        baseline_quality_score=None,
                        model_calls=1,
                        baseline_model_calls=None,
                        employee_count=2,
                        maximum_parallelism=1,
                        writer_count=0,
                        approvals_requested=0,
                        approvals_granted=0,
                        preapproval_mutations=0,
                        validation_attempts=(True,),
                        ledger_digest=f"ledger-{suffix}",
                    )
                    store.record_episode(episode)
                    store.record_staffing_demand(
                        StaffingDemandEvidence.create(
                            episode_id=episode.episode_id,
                            job_id=episode.job_id,
                            source=episode.source,
                            context_fingerprint=context,
                            execution_profile="READ_ONLY",
                            base_roster_revision=2,
                            task_id="security",
                            capability="security_review",
                            role_label="Temporary Security Review Specialist",
                            job_succeeded=True,
                            validation_attempts=(True,),
                            safety_violations=(),
                            writer_count=0,
                            approvals_requested=0,
                            approvals_granted=0,
                            preapproval_mutations=0,
                            ledger_digest=episode.ledger_digest,
                            recorded_at=episode.recorded_at,
                        )
                    )
                candidate = HiringRecommendationService(store).curate().candidates[0]
                patches = RosterPatchService(store)
                patches.approve(candidate.patch_id, actor="user:test")
                patches.apply(candidate.patch_id, actor="user:test")
                contract = store.get_hire_observation_contract(candidate.patch_id)

            output = io.StringIO()
            error = io.StringIO()
            exit_code = main(
                [
                    "run",
                    "Use the security reviewer",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(state),
                    "--provider",
                    "openai-api",
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "session-model",
                    "--no-auth",
                    "--json",
                ],
                provider_factory=lambda config: RosterCapturingProvider(
                    capability="security_review"
                ),
                stdout=output,
                stderr=error,
            )
            with CompanyStateStore(state) as store:
                observations = store.list_hire_observations(candidate.patch_id)
                assessments = store.list_hire_assessments(candidate.patch_id)
                roster_revision = store.roster().revision

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(len(observations), 1)
        self.assertTrue(observations[0].attribution_eligible)
        self.assertTrue(observations[0].cohort_eligible)
        self.assertTrue(observations[0].persistent_employee_assigned)
        self.assertFalse(observations[0].temporary_fallback_used)
        self.assertEqual(observations[0].base_roster_revision, 3)
        self.assertEqual(observations[0].patch_id, contract.patch_id)
        self.assertEqual(assessments, ())
        self.assertEqual(roster_revision, 3)

    def test_company_cli_requires_confirmation_and_preserves_patch_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "runtime.db"
            config = Path(temporary) / "missing.toml"
            with CompanyStateStore(state) as store:
                store.record_episode(company_episode("cli-one"))
                store.record_episode(company_episode("cli-two"))
            curate_output = io.StringIO()
            error = io.StringIO()
            curate_code = main(
                [
                    "--config",
                    str(config),
                    "company",
                    "curate",
                    "--state",
                    str(state),
                    "--json",
                ],
                stdout=curate_output,
                stderr=error,
            )
            patch_id = json.loads(curate_output.getvalue())["candidates"][0]["patch_id"]

            preview_output = io.StringIO()
            preview_code = main(
                [
                    "--config",
                    str(config),
                    "company",
                    "preview",
                    patch_id,
                    "--state",
                    str(state),
                ],
                stdout=preview_output,
                stderr=error,
            )

            denied_code = main(
                [
                    "--config",
                    str(config),
                    "company",
                    "approve",
                    patch_id,
                    "--state",
                    str(state),
                ],
                stdout=io.StringIO(),
                stderr=error,
            )
            with CompanyStateStore(state) as store:
                self.assertEqual(store.get_patch(patch_id).status, WorkflowPatchStatus.PROPOSED)

            for command in ("approve", "apply"):
                code = main(
                    [
                        "--config",
                        str(config),
                        "company",
                        command,
                        patch_id,
                        "--state",
                        str(state),
                        "--confirm",
                        "--json",
                    ],
                    stdout=io.StringIO(),
                    stderr=error,
                )
                self.assertEqual(code, EXIT_OK, error.getvalue())
            replay_output = io.StringIO()
            replay_code = main(
                [
                    "--config",
                    str(config),
                    "company",
                    "replay",
                    patch_id,
                    "--state",
                    str(state),
                    "--json",
                ],
                stdout=replay_output,
                stderr=error,
            )
            rollback_code = main(
                [
                    "--config",
                    str(config),
                    "company",
                    "rollback",
                    patch_id,
                    "--state",
                    str(state),
                    "--confirm",
                    "--json",
                ],
                stdout=io.StringIO(),
                stderr=error,
            )
            with CompanyStateStore(state) as store:
                final = store.get_patch(patch_id)
                playbook = store.playbook()

        self.assertEqual(curate_code, EXIT_OK, error.getvalue())
        self.assertEqual(preview_code, EXIT_OK, error.getvalue())
        self.assertIn("Expected: quality +", preview_output.getvalue())
        self.assertIn("implement_change", preview_output.getvalue())
        self.assertIn("Lifecycle: PROPOSED", preview_output.getvalue())
        self.assertEqual(denied_code, EXIT_INPUT)
        self.assertIn("requires --confirm", error.getvalue())
        self.assertEqual(replay_code, EXIT_OK)
        self.assertTrue(json.loads(replay_output.getvalue())["replay_matches"])
        self.assertEqual(rollback_code, EXIT_OK)
        self.assertEqual(final.status, WorkflowPatchStatus.ROLLED_BACK)
        self.assertEqual(playbook.revision, 3)
        self.assertEqual(playbook.patterns, ())

    def test_applied_playbook_does_not_force_preflight_topology(self) -> None:
        context = fixture_workflow_context()
        task = WorkflowTaskTemplate(
            "analyze",
            ("repository_analysis",),
            final=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "runtime.db"
            with CompanyStateStore(state) as store:
                for suffix in ("one", "two"):
                    store.record_episode(
                        OrganizationEpisode.create(
                            job_id=f"prior-{suffix}",
                            source=EvidenceSource.LIVE_EVALUATION,
                            task_family="repository.read-only-analysis",
                            context_fingerprint=context,
                            execution_profile="READ_ONLY",
                            planning_mode="SOLO",
                            plan_template=(task,),
                            success=True,
                            quality_score=1.0,
                            baseline_quality_score=0.8,
                            model_calls=1,
                            baseline_model_calls=2,
                            employee_count=1,
                            maximum_parallelism=1,
                            writer_count=0,
                            approvals_requested=0,
                            approvals_granted=0,
                            preapproval_mutations=0,
                            validation_attempts=(True,),
                            ledger_digest=f"ledger-{suffix}",
                        )
                    )
                learning = CompanyLearningService(store)
                patch = learning.curate().candidates[0]
                learning.approve(patch.patch_id, actor="user:test")
                learning.apply(patch.patch_id, actor="user:test")

            provider = PriorCapturingProvider()
            output = io.StringIO()
            error = io.StringIO()
            exit_code = main(
                [
                    "run",
                    "Inspect the repository",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(state),
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "contract-model",
                    "--no-auth",
                    "--json",
                ],
                provider_factory=lambda config: provider,
                stdout=output,
                stderr=error,
            )

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(provider.structured_requests, [])
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["planning_mode"], "SOLO")
        self.assertEqual(payload["planning_reason"], "SOLO_FIRST_ATTEMPT")
        self.assertEqual(payload["compiler_usage"]["model_calls"], 0)

    def test_large_repository_prior_replays_only_after_matching_typed_gap(self) -> None:
        learned_plan = (
            WorkflowTaskTemplate(
                "security_evidence",
                ("security_review",),
            ),
            WorkflowTaskTemplate(
                "integrate_findings",
                ("repository_analysis",),
                depends_on=("security_evidence",),
                final=True,
            ),
        )
        provider = ScriptedModelProvider(
            [
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="Initial repository analysis found a typed review boundary.",
                        signals=(
                            RunSignal(
                                SignalCode.CAPABILITY_MISSING,
                                "security_review",
                                ("specialist evidence is required",),
                            ),
                        ),
                    )
                ),
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="Verified security evidence."
                    )
                ),
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="Integrated prior-guided company result."
                    )
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "large-workspace"
            workspace.mkdir()
            (workspace / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            for index in range(501):
                (workspace / f"module-{index:03d}.py").touch()
            context = workflow_context_fingerprint_v2(
                project_workspace_structure(workspace, "READ_ONLY")
            )
            state = root / "runtime.db"
            with CompanyStateStore(state) as store:
                for suffix in ("one", "two"):
                    store.record_episode(
                        OrganizationEpisode.create(
                            job_id=f"gap-prior-{suffix}",
                            source=EvidenceSource.LIVE_EVALUATION,
                            task_family="repository.security-review-gap",
                            context_fingerprint=context,
                            execution_profile="READ_ONLY",
                            planning_mode="DYNAMIC",
                            plan_template=learned_plan,
                            success=True,
                            quality_score=1.0,
                            baseline_quality_score=0.7,
                            model_calls=2,
                            baseline_model_calls=3,
                            employee_count=2,
                            maximum_parallelism=1,
                            writer_count=0,
                            approvals_requested=0,
                            approvals_granted=0,
                            preapproval_mutations=0,
                            validation_attempts=(True,),
                            ledger_digest=f"gap-ledger-{suffix}",
                        )
                    )
                learning = CompanyLearningService(store)
                candidate = learning.curate().candidates[0]
                learning.approve(candidate.patch_id, actor="user:test")
                learning.apply(candidate.patch_id, actor="user:test")
                patch_id = candidate.patch_id

            output = io.StringIO()
            error = io.StringIO()
            exit_code = main(
                [
                    "run",
                    "Inspect the repository security boundary",
                    "--workspace",
                    str(workspace),
                    "--state",
                    str(state),
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "contract-model",
                    "--no-auth",
                    "--json",
                ],
                provider_factory=lambda config: provider,
                stdout=output,
                stderr=error,
            )
            with CompanyStateStore(state) as store:
                observations = store.list_observations(patch_id)

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["planning_mode"], "SOLO")
        self.assertEqual(payload["planning_reason"], "SOLO_FIRST_ATTEMPT")
        self.assertEqual(payload["compiler_usage"]["model_calls"], 0)
        self.assertEqual(payload["metrics"]["graph_patch_count"], 1)
        self.assertEqual(payload["metrics"]["organization_admission_count"], 1)
        self.assertEqual(payload["summary"], "Integrated prior-guided company result.")
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(len(observations), 1)
        self.assertTrue(observations[0].prior_exposed)
        self.assertTrue(observations[0].proposal_aligned)

    def test_company_cli_roster_patch_requires_confirmation_and_changes_next_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "runtime.db"
            config = Path(temporary) / "missing.toml"
            with CompanyStateStore(state) as store:
                store.ensure_roster_baseline(
                    (
                        EmployeeRecord(
                            "employee-generalist",
                            "Generalist",
                            ("conversation",),
                            model_profile="company-default",
                        ),
                    )
                )
            proposed_output = io.StringIO()
            error = io.StringIO()
            proposed_code = main(
                [
                    "--config",
                    str(config),
                    "company",
                    "roster-propose",
                    "ADD_EMPLOYEE",
                    "--employee-id",
                    "employee-reviewer",
                    "--role",
                    "Reviewer",
                    "--capability",
                    "review",
                    "--rationale",
                    "Repeated review work needs a stable employee.",
                    "--state",
                    str(state),
                    "--json",
                ],
                stdout=proposed_output,
                stderr=error,
            )
            proposal = json.loads(proposed_output.getvalue())
            patch_id = proposal["patch"]["patch_id"]

            denied_code = main(
                [
                    "--config",
                    str(config),
                    "company",
                    "roster-approve",
                    patch_id,
                    "--state",
                    str(state),
                ],
                stdout=io.StringIO(),
                stderr=error,
            )
            with CompanyStateStore(state) as store:
                self.assertEqual(store.roster().revision, 2)
                self.assertEqual(
                    store.get_roster_patch(patch_id).status,
                    RosterPatchStatus.PROPOSED,
                )

            for command in ("roster-approve", "roster-apply"):
                code = main(
                    [
                        "--config",
                        str(config),
                        "company",
                        command,
                        patch_id,
                        "--state",
                        str(state),
                        "--confirm",
                        "--json",
                    ],
                    stdout=io.StringIO(),
                    stderr=error,
                )
                self.assertEqual(code, EXIT_OK, error.getvalue())

            preview_output = io.StringIO()
            preview_code = main(
                [
                    "--config",
                    str(config),
                    "company",
                    "roster-preview",
                    patch_id,
                    "--state",
                    str(state),
                ],
                stdout=preview_output,
                stderr=error,
            )
            status_output = io.StringIO()
            status_code = main(
                [
                    "--config",
                    str(config),
                    "company",
                    "status",
                    "--state",
                    str(state),
                ],
                stdout=status_output,
                stderr=error,
            )
            provider = RosterCapturingProvider(capability="review")
            tui_output = TtyStringIO()
            tui_code = main(
                [
                    "chat",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(state),
                    "--provider",
                    "openai-api",
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "session-model",
                    "--no-auth",
                ],
                provider_factory=lambda provider_config: provider,
                stdin=TtyStringIO("/quit\n"),
                stdout=tui_output,
                stderr=error,
            )
            next_job_output = io.StringIO()
            next_job_code = main(
                [
                    "run",
                    "Use the newly approved reviewer",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(state),
                    "--provider",
                    "openai-api",
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "session-model",
                    "--no-auth",
                    "--json",
                ],
                provider_factory=lambda provider_config: provider,
                stdout=next_job_output,
                stderr=error,
            )
            with CompanyStateStore(state) as store:
                snapshot = decode_active_roster(store.roster())
                events = store.list_roster_patch_events(patch_id)

        self.assertEqual(proposed_code, EXIT_OK, error.getvalue())
        self.assertFalse(proposal["state_changed"])
        self.assertEqual(proposal["active_roster_revision"], 2)
        self.assertEqual(denied_code, EXIT_INPUT)
        self.assertIn("requires --confirm", error.getvalue())
        self.assertEqual(preview_code, EXIT_OK, error.getvalue())
        self.assertIn("Before: none", preview_output.getvalue())
        self.assertIn("Lifecycle: PROPOSED → APPROVED → APPLIED", preview_output.getvalue())
        self.assertIn("Active ROSTER changed: no", preview_output.getvalue())
        self.assertEqual(status_code, EXIT_OK, error.getvalue())
        self.assertIn("ROSTER r3 (2 active / 2 total)", status_output.getvalue())
        self.assertIn("Roster patches: proposed=0 · applied=1", status_output.getvalue())
        self.assertEqual(tui_code, EXIT_OK, error.getvalue())
        self.assertIn("roster        r3 · 2 active", tui_output.getvalue())
        self.assertIn("employees     Generalist · Reviewer", tui_output.getvalue())
        self.assertEqual(next_job_code, EXIT_OK, error.getvalue())
        self.assertIn("employee-reviewer", str(provider.requests[0].messages))
        self.assertIn("Reviewer", str(provider.requests[0].messages))
        self.assertEqual(provider.structured_requests, [])
        self.assertEqual(snapshot.revision, 3)
        self.assertEqual(
            {employee.employee_id for employee in snapshot.employees},
            {"employee-generalist", "employee-reviewer"},
        )
        self.assertEqual(
            [event.event_type.value for event in events],
            ["PROPOSED", "APPROVED", "APPLIED"],
        )

    def test_company_cli_employee_skill_is_scoped_and_approval_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "runtime.db"
            error = io.StringIO()
            with CompanyStateStore(state) as store:
                store.ensure_roster_baseline(
                    (
                        EmployeeRecord(
                            "employee-repository-analyst",
                            "Repository Analyst",
                            ("repository_analysis",),
                        ),
                        EmployeeRecord(
                            "employee-engineer",
                            "Engineer",
                            ("implementation",),
                        ),
                    )
                )
            policy_code = main(
                [
                    "company",
                    "review-policy-set",
                    "always-approve",
                    "--state",
                    str(state),
                    "--confirm",
                    "--json",
                ],
                stdout=io.StringIO(),
                stderr=error,
            )
            proposal_args = [
                "company",
                "skill-propose",
                "--employee-id",
                "employee-repository-analyst",
                "--skill-key",
                "targeted-validation",
                "--context-key",
                "tiny-python-repository",
                "--purpose",
                "Validate the smallest relevant surface first.",
                "--step",
                "Identify the affected behavior.",
                "--step",
                "Run narrow validation before the full suite.",
                "--verify",
                "Confirm narrow validation and the full suite pass.",
                "--prohibition",
                "Do not skip required approval.",
                "--correction-id",
                "user-correction-cli-001",
                "--rationale",
                "The user confirmed this reusable procedure.",
                "--state",
                str(state),
                "--json",
            ]
            denied_code = main(proposal_args, stdout=io.StringIO(), stderr=error)
            proposal_output = io.StringIO()
            proposed_code = main(
                [*proposal_args, "--confirm"],
                stdout=proposal_output,
                stderr=error,
            )
            proposal = json.loads(proposal_output.getvalue())
            patch_id = proposal["patch"]["patch_id"]
            with CompanyStateStore(state) as store:
                before_apply = store.list_employee_skills()
                proposed_status = store.get_employee_skill_patch(patch_id).status
            approve_code = main(
                [
                    "company",
                    "skill-approve",
                    patch_id,
                    "--state",
                    str(state),
                    "--confirm",
                    "--json",
                ],
                stdout=io.StringIO(),
                stderr=error,
            )
            apply_output = io.StringIO()
            apply_code = main(
                [
                    "company",
                    "skill-apply",
                    patch_id,
                    "--state",
                    str(state),
                    "--confirm",
                    "--json",
                ],
                stdout=apply_output,
                stderr=error,
            )
            skills_output = io.StringIO()
            skills_code = main(
                [
                    "company",
                    "employee-skills",
                    "--employee-id",
                    "employee-repository-analyst",
                    "--context-key",
                    "tiny-python-repository",
                    "--state",
                    str(state),
                    "--json",
                ],
                stdout=skills_output,
                stderr=error,
            )

        applied = json.loads(apply_output.getvalue())
        listed = json.loads(skills_output.getvalue())
        self.assertEqual(policy_code, EXIT_OK, error.getvalue())
        self.assertEqual(denied_code, EXIT_INPUT)
        self.assertIn("requires --confirm", error.getvalue())
        self.assertEqual(proposed_code, EXIT_OK, error.getvalue())
        self.assertEqual(proposed_status, EmployeeSkillPatchStatus.PROPOSED)
        self.assertEqual(before_apply, ())
        self.assertFalse(proposal["active_skill_changed"])
        self.assertEqual(proposal["review_mode"], "approval")
        self.assertEqual(approve_code, EXIT_OK, error.getvalue())
        self.assertEqual(apply_code, EXIT_OK, error.getvalue())
        self.assertEqual(applied["status"], "APPLIED")
        self.assertEqual(skills_code, EXIT_OK, error.getvalue())
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["revision"], 1)
        self.assertEqual(listed[0]["procedure"]["employee_id"], "employee-repository-analyst")

    def test_employee_skill_evaluation_cli_is_offline(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        exit_code = main(
            ["eval", "employee-skill", "--json"],
            stdout=output,
            stderr=error,
        )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["provider_calls"], 0)
        self.assertFalse(payload["quota_consumed"])
        self.assertTrue(all(check["passed"] for check in payload["checks"]))

    def test_task_mutation_evaluation_cli_is_offline_and_replayable(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        exit_code = main(
            ["eval", "task-mutation", "--json"],
            stdout=output,
            stderr=error,
        )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(payload["schema_version"], "noruct.task-mutation-evaluation.v1")
        self.assertEqual(payload["retry"]["mutations"], ["RETRY"])
        self.assertEqual(payload["reroute"]["mutations"], ["REROUTE"])
        self.assertTrue(payload["deterministic_replay"])
        self.assertEqual(payload["provider_calls"], 0)
        self.assertFalse(payload["quota_consumed"])
        self.assertTrue(all(check["passed"] for check in payload["checks"]))

    def test_active_job_ledger_evaluation_cli_is_offline_and_replayable(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        exit_code = main(
            ["eval", "active-job-ledger", "--json"],
            stdout=output,
            stderr=error,
        )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(
            payload["schema_version"],
            "noruct.active-job-ledger-evaluation.v1",
        )
        self.assertEqual(payload["retry"]["audit_status"], "TERMINAL")
        self.assertEqual(payload["reroute"]["audit_status"], "TERMINAL")
        self.assertEqual(payload["interrupted"]["audit_status"], "INTERRUPTED")
        self.assertEqual(payload["provider_calls"], 0)
        self.assertFalse(payload["quota_consumed"])
        self.assertTrue(all(check["passed"] for check in payload["checks"]))

    def test_manager_campaign_rehearsal_cli_is_complete_and_quota_free(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        exit_code = main(
            ["eval", "manager-campaign", "rehearse", "--json"],
            stdout=output,
            stderr=error,
        )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(
            payload["schema_version"],
            "noruct.manager-value-offline-rehearsal.v1",
        )
        self.assertTrue(payload["passed"])
        self.assertEqual(len(payload["outcomes"]), 16)
        self.assertEqual(payload["external_model_calls"], 0)
        self.assertFalse(payload["quota_consumed"])

    def test_applied_employee_skill_reaches_runtime_and_is_observed(self) -> None:
        context = fixture_workflow_context()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "runtime.db"
            with CompanyStateStore(state) as store:
                store.ensure_roster_baseline(
                    (
                        EmployeeRecord(
                            "employee-repository-analyst",
                            "Repository Analyst",
                            ("repository_analysis",),
                        ),
                        EmployeeRecord(
                            "employee-generalist",
                            "Generalist",
                            ("conversation",),
                        ),
                    )
                )
                service = EmployeeSkillPatchService(store)
                candidate = service.propose_user_correction(
                    EmployeeSkillProcedure(
                        employee_id="employee-repository-analyst",
                        skill_key="targeted-validation",
                        context_key=context,
                        purpose="Validate the smallest relevant surface first.",
                        steps=("Run the narrow validation before the full suite.",),
                        verification_steps=("Confirm the full suite passes.",),
                    ),
                    correction_id="runtime-correction-001",
                    rationale="Verify the real company runtime binding.",
                    actor="user:test",
                )
                service.approve(candidate.patch_id, actor="user:test")
                service.apply(candidate.patch_id, actor="user:test")

            provider = RosterCapturingProvider(capability="repository_analysis")
            output = io.StringIO()
            error = io.StringIO()
            exit_code = main(
                [
                    "run",
                    "Inspect the repository structure",
                    "--workspace",
                    str(FIXTURE_ROOT),
                    "--state",
                    str(state),
                    "--provider",
                    "openai-api",
                    "--base-url",
                    "http://127.0.0.1:9/v1",
                    "--model",
                    "contract-model",
                    "--no-auth",
                    "--json",
                ],
                provider_factory=lambda config: provider,
                stdout=output,
                stderr=error,
            )
            with CompanyStateStore(state) as store:
                observations = store.list_employee_skill_observations(
                    candidate.patch_id
                )

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(provider.call_count, 1)
        self.assertIn(
            "employee-skill:employee-repository-analyst:targeted-validation",
            str(provider.requests[0].messages),
        )
        self.assertEqual(len(observations), 1)
        self.assertTrue(observations[0].skill_exposed)
        self.assertTrue(observations[0].attribution_eligible)

    def test_source_backed_managed_skill_cli_has_explicit_root_and_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "managed-skills"
            content_path = Path(temporary) / "skill.md"
            content_path.write_text(
                "---\nname: demo-skill\ndescription: Managed demo\n---\n\n"
                "# Demo\nUse the verified procedure.\n",
                encoding="utf-8",
            )
            revised_content_path = Path(temporary) / "revised-skill.md"
            revised_content_path.write_text(
                "---\nname: demo-skill\ndescription: Managed demo revised\n---\n\n"
                "# Demo\nUse the verified procedure carefully.\n",
                encoding="utf-8",
            )
            support_path = Path(temporary) / "checklist.md"
            support_path.write_text("- inspect\n- verify\n", encoding="utf-8")
            error = io.StringIO()
            denied = main(
                [
                    "skills",
                    "manage",
                    "create",
                    "demo-skill",
                    "--skills-root",
                    str(root),
                    "--content-file",
                    str(content_path),
                    "--json",
                ],
                stdout=io.StringIO(),
                stderr=error,
            )
            create_output = io.StringIO()
            created = main(
                [
                    "skills",
                    "manage",
                    "create",
                    "demo-skill",
                    "--skills-root",
                    str(root),
                    "--content-file",
                    str(content_path),
                    "--confirm",
                    "--json",
                ],
                stdout=create_output,
                stderr=error,
            )
            support_written = main(
                [
                    "skills",
                    "manage",
                    "write_file",
                    "demo-skill",
                    "--skills-root",
                    str(root),
                    "--file-path",
                    "references/checklist.md",
                    "--file-content-file",
                    str(support_path),
                    "--confirm",
                    "--json",
                ],
                stdout=io.StringIO(),
                stderr=error,
            )
            support_removed = main(
                [
                    "skills",
                    "manage",
                    "remove_file",
                    "demo-skill",
                    "--skills-root",
                    str(root),
                    "--file-path",
                    "references/checklist.md",
                    "--confirm",
                    "--json",
                ],
                stdout=io.StringIO(),
                stderr=error,
            )
            edited = main(
                [
                    "skills",
                    "manage",
                    "edit",
                    "demo-skill",
                    "--skills-root",
                    str(root),
                    "--content-file",
                    str(revised_content_path),
                    "--confirm",
                    "--json",
                ],
                stdout=io.StringIO(),
                stderr=error,
            )
            patch_output = io.StringIO()
            patched = main(
                [
                    "skills",
                    "manage",
                    "patch",
                    "demo-skill",
                    "--skills-root",
                    str(root),
                    "--old-text",
                    "verified procedure carefully.",
                    "--new-text",
                    "verified procedure always.",
                    "--confirm",
                    "--json",
                ],
                stdout=patch_output,
                stderr=error,
            )
            list_output = io.StringIO()
            listed = main(
                ["skills", "list", "--skills-dir", str(root), "--json"],
                stdout=list_output,
                stderr=error,
            )
            deleted = main(
                [
                    "skills",
                    "manage",
                    "delete",
                    "demo-skill",
                    "--skills-root",
                    str(root),
                    "--confirm",
                    "--json",
                ],
                stdout=io.StringIO(),
                stderr=error,
            )

        create_payload = json.loads(create_output.getvalue())
        patch_payload = json.loads(patch_output.getvalue())
        list_payload = json.loads(list_output.getvalue())
        self.assertEqual(denied, EXIT_INPUT)
        self.assertIn("require --confirm", error.getvalue())
        self.assertEqual(created, EXIT_OK, error.getvalue())
        self.assertTrue(create_payload["source"]["success"])
        self.assertTrue(create_payload["changed"])
        self.assertNotEqual(
            create_payload["before"]["tree_sha256"],
            create_payload["after"]["tree_sha256"],
        )
        self.assertEqual(support_written, EXIT_OK, error.getvalue())
        self.assertEqual(support_removed, EXIT_OK, error.getvalue())
        self.assertEqual(edited, EXIT_OK, error.getvalue())
        self.assertEqual(patched, EXIT_OK, error.getvalue())
        self.assertTrue(patch_payload["source"]["success"])
        self.assertEqual(listed, EXIT_OK, error.getvalue())
        self.assertEqual(list_payload["discovered_count"], 1)
        self.assertEqual(list_payload["skills"][0]["name"], "demo-skill")
        self.assertEqual(deleted, EXIT_OK, error.getvalue())


if __name__ == "__main__":
    unittest.main()
