from __future__ import annotations

import asyncio
import json
import stat
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from dynamic_firm.application.modern_terminal_controller import ModernInteractiveController
from dynamic_firm.application.modern_terminal_integrations import (
    execute_integration_command,
)
from dynamic_firm.application.modern_terminal_graph import (
    apply_graph_blueprint_action,
    apply_graph_control,
    graph_control_snapshot,
)
from dynamic_firm.application.modern_terminal_knowledge import (
    execute_knowledge_command,
)
from dynamic_firm.application.modern_terminal_job_audit import (
    _frozen_route_admission_projection,
    _frozen_budget_envelope,
    _graph_change_summary,
    _model_invocation_receipt_projection,
    _observed_execution,
    _read_only_continuation_candidate,
    execute_job_audit_command,
    job_audit_catalog,
    job_audit_snapshot,
)
from dynamic_firm.company.model_invocation_receipt import ModelInvocationReceipt
from dynamic_firm.application.modern_terminal_settings import (
    execute_runtime_settings_command,
)
from dynamic_firm.company.user_routing_policy import (
    ApprovedRouteMetadata,
    ApprovedRouteRegistry,
    UserRoutingPolicy,
    UserRoutingPolicyMode,
)
from dynamic_firm.cli import _modern_controller_ports, build_parser
from dynamic_firm.product.local_routing_settings import (
    LocalRoutingSettings,
    load_local_routing_settings,
    write_local_routing_settings,
)


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "tiny_repo"


class ModernTerminalControllerBoundaryTests(unittest.TestCase):
    def test_portfolio_tui_commands_reuse_cli_authority_without_building_a_provider(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = build_parser().parse_args(
                [
                    "--config", str(root / "config.toml"),
                    "chat", "--workspace", str(FIXTURE_ROOT),
                    "--state", str(root / "runtime.db"),
                    "--provider", "openai-api", "--base-url", "http://127.0.0.1:9/v1",
                    "--model", "portfolio-tui", "--no-auth", "--permission-mode", "read-only",
                ]
            )
            controller = ModernInteractiveController(
                args,
                {},
                provider_factory=lambda _config: self.fail("portfolio submit/status must not build a provider"),
                coding_worker_factory=lambda _config: self.fail("portfolio submit/status must not build a coding worker"),
                ports=_modern_controller_ports(),
            )
            try:
                before = asyncio.run(controller.execute_command("/portfolio status"))
                submitted = asyncio.run(
                    controller.execute_command("/portfolio submit --confirm inspect repository")
                )
                after = asyncio.run(controller.execute_command("/portfolio status"))
                self.assertIn("No local Work Orders", "\n".join(before.messages))
                self.assertIn("queued", "\n".join(submitted.messages))
                self.assertIn("QUEUED", "\n".join(after.messages))
                self.assertEqual(controller.turn_count, 0)
            finally:
                controller.close()

    def test_network_guidance_stays_local_and_does_not_build_a_provider(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = build_parser().parse_args(
                [
                    "--config", str(root / "config.toml"),
                    "chat", "--workspace", str(FIXTURE_ROOT),
                    "--state", str(root / "runtime.db"),
                    "--provider", "openai-api", "--base-url", "http://127.0.0.1:9/v1",
                    "--model", "network-guidance", "--no-auth", "--permission-mode", "read-only",
                ]
            )
            controller = ModernInteractiveController(
                args,
                {},
                provider_factory=lambda _config: self.fail("Network guidance must not build a provider"),
                coding_worker_factory=lambda _config: self.fail("Network guidance must not build a coding worker"),
                ports=_modern_controller_ports(),
            )
            try:
                install = asyncio.run(controller.execute_command("/network install"))
                permissions = asyncio.run(controller.execute_command("/network permissions"))
                self.assertIn("noruct network install", "\n".join(install.messages))
                self.assertIn("cannot add credentials", "\n".join(permissions.messages))
                self.assertEqual(controller.turn_count, 0)
            finally:
                controller.close()

    def test_network_operator_commands_use_the_shared_local_lifecycle(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed = root / "allowed-signers"
            allowed.write_text("fixture signer", encoding="utf-8")
            args = build_parser().parse_args(
                [
                    "--config", str(root / "config.toml"),
                    "chat", "--workspace", str(FIXTURE_ROOT),
                    "--state", str(root / "runtime.db"),
                    "--provider", "openai-api", "--base-url", "http://127.0.0.1:9/v1",
                    "--model", "network-operator", "--no-auth", "--permission-mode", "read-only",
                ]
            )
            controller = ModernInteractiveController(
                args, {},
                provider_factory=lambda _config: self.fail("Network operator commands must not build a provider"),
                coding_worker_factory=lambda _config: self.fail("Network operator commands must not build a coding worker"),
                ports=_modern_controller_ports(),
            )
            try:
                payload = {
                    "confirm": True, "source_id": "fixture_community", "publisher_class": "COMMUNITY",
                    "origin": "https://network.example", "allowed_signers_path": str(allowed),
                    "signer_principal": "fixture_signer", "ssh_keygen_path": sys.executable,
                    "operator_id": "fixture_operator", "credential_env": None,
                    "private_registry_id": None, "allow_insecure_loopback": False,
                }
                saved = asyncio.run(controller.execute_command("/network source-add " + json.dumps(payload)))
                listed = asyncio.run(controller.execute_command("/network sources"))
                self.assertIn("Trusted Network source saved", "\n".join(saved.messages))
                self.assertIn("fixture_community · COMMUNITY", "\n".join(listed.messages))
                self.assertEqual(controller.turn_count, 0)
            finally:
                controller.close()

    def test_public_controller_runs_local_knowledge_without_building_a_provider(self) -> None:
        """The application controller stays reusable while CLI owns its ports."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = build_parser().parse_args(
                [
                    "--config", str(root / "config.toml"),
                    "chat", "--workspace", str(FIXTURE_ROOT),
                    "--state", str(root / "runtime.db"),
                    "--provider", "openai-api", "--base-url", "http://127.0.0.1:9/v1",
                    "--model", "controller-contract", "--no-auth", "--permission-mode", "read-only",
                ]
            )
            controller = ModernInteractiveController(
                args,
                {},
                provider_factory=lambda _config: self.fail("local command must not build a provider"),
                coding_worker_factory=lambda _config: self.fail("local command must not build a coding worker"),
                ports=_modern_controller_ports(),
            )
            try:
                remembered = asyncio.run(
                    controller.execute_command("/remember application-controller boundary evidence")
                )
                retrieved = asyncio.run(controller.execute_command("/knowledge boundary"))
                self.assertIn("Remembered locally", "\n".join(remembered.messages))
                self.assertIn("application-controller boundary evidence", "\n".join(retrieved.messages))
                self.assertEqual(controller.turn_count, 0)
            finally:
                controller.close()

    def test_integration_component_uses_host_rebuild_path_without_cli_import(self) -> None:
        """A connector save stays a local setting until the host rebuilds it."""

        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            loaded: list[Path] = []
            rebuilds: list[bool] = []

            class Host:
                config = SimpleNamespace(config_path=config_path)
                settings: dict[str, object] = {}
                ports = SimpleNamespace(
                    load_config=lambda path: loaded.append(path) or {"web_search": {"base_url": "http://127.0.0.1:8080"}}
                )

                def _persist_global_runtime_defaults(self) -> None:
                    rebuilds.append(True)

            result = execute_integration_command(
                Host(),
                "/quick-web-search",
                "http://127.0.0.1:8080",
            )

            self.assertIsNotNone(result)
            self.assertIn("Web search connected", "\n".join(result.messages))
            self.assertEqual([item.resolve() for item in loaded], [config_path.resolve()])
            self.assertEqual(rebuilds, [True])
            self.assertIn("127.0.0.1:8080", config_path.read_text(encoding="utf-8"))

    def test_quick_plugin_intake_stays_inactive_until_separate_activation(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.toml"
            source = root / "plugin-source"
            source.mkdir()
            (source / "noruct-plugin.json").write_text(
                json.dumps(
                    {
                        "schema": "noruct.executable-plugin.v1",
                        "plugin_id": "echo",
                        "version": "1.0.0",
                        "description": "Inactive intake fixture.",
                        "command": ["host.py"],
                        "environment": [],
                        "timeout_seconds": 5,
                        "tools": [
                            {
                                "name": "plugin_echo_reply",
                                "description": "Echo safely.",
                                "input_schema": {
                                    "type": "object",
                                    "properties": {},
                                    "required": [],
                                    "additionalProperties": False,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            host = source / "host.py"
            host.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            host.chmod(host.stat().st_mode | stat.S_IXUSR)

            class Host:
                config = SimpleNamespace(config_path=config_path)
                settings: dict[str, object] = {}
                ports = SimpleNamespace(
                    plugin_root=lambda path: path.parent / "plugins",
                    load_config=lambda _path: {"plugins": {"enabled": True}},
                )

                def _persist_global_runtime_defaults(self) -> None:
                    return None

            result = execute_integration_command(
                Host(), "/quick-plugin", str(source)
            )
            self.assertIsNotNone(result)
            self.assertIn("installed inactive", "\n".join(result.messages))
            self.assertIn("plugin enable echo --version 1.0.0", "\n".join(result.messages))
            from dynamic_firm.product.executable_plugins import ExecutablePluginStore

            installed = ExecutablePluginStore(root / "plugins").list()
            self.assertEqual(len(installed), 1)
            self.assertFalse(installed[0].enabled)
            self.assertEqual(ExecutablePluginStore(root / "plugins").active(), ())

    def test_graph_component_projects_and_saves_only_future_job_preferences(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = build_parser().parse_args(
                [
                    "--config", str(root / "config.toml"),
                    "chat", "--workspace", str(FIXTURE_ROOT),
                    "--state", str(root / "runtime.db"),
                    "--provider", "openai-api", "--base-url", "http://127.0.0.1:9/v1",
                    "--model", "graph-component", "--no-auth", "--permission-mode", "read-only",
                ]
            )
            controller = ModernInteractiveController(
                args,
                {},
                provider_factory=lambda _config: self.fail("graph preferences do not build a provider"),
                coding_worker_factory=lambda _config: self.fail("graph preferences do not build a coding worker"),
                ports=_modern_controller_ports(),
            )
            try:
                before = graph_control_snapshot(controller.state_path)
                result = apply_graph_control(
                    controller,
                    {
                        "blueprint_id": None,
                        "version": None,
                        "pinned_employee_ids": (),
                        "excluded_employee_ids": (),
                        "require_independent_review": True,
                        "max_concurrency": 1,
                        "max_cost_usd": 0.5,
                        "max_wall_time_ms": 10_000,
                        "mutation_policy": "PROPOSE",
                    },
                )
                after = graph_control_snapshot(controller.state_path)
                self.assertEqual(before["selection"]["mutation_policy"], "BOUNDED_AUTO")
                self.assertIn("Future Job Graph defaults saved", "\n".join(result))
                self.assertEqual(after["selection"]["mutation_policy"], "PROPOSE")
                self.assertEqual(after["selection"]["max_concurrency"], 1)
                preview = controller.preview_graph("Validate a future release")
                self.assertIn("No Graph Blueprint is selected", "\n".join(preview))
            finally:
                controller.close()

    def test_graph_workbench_authors_forks_and_revises_without_provider_or_selection(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = build_parser().parse_args(
                [
                    "--config", str(root / "config.toml"),
                    "chat", "--workspace", str(FIXTURE_ROOT),
                    "--state", str(root / "runtime.db"),
                    "--provider", "openai-api", "--base-url", "http://127.0.0.1:9/v1",
                    "--model", "graph-workbench", "--no-auth", "--permission-mode", "read-only",
                ]
            )
            controller = ModernInteractiveController(
                args,
                {},
                provider_factory=lambda _config: self.fail("Blueprint authoring must not build a provider"),
                coding_worker_factory=lambda _config: self.fail("Blueprint authoring must not build a coding worker"),
                ports=_modern_controller_ports(),
            )
            try:
                created = apply_graph_blueprint_action(
                    controller,
                    {
                        "action": "create_draft",
                        "blueprint_id": "local_review",
                        "objective_class": "general",
                        "execution_profiles": "read_only",
                        "required_capabilities": "analysis",
                        "objective_template": "Review {{objective}}",
                        "acceptance_template": "Answer {{requested_outcome}}",
                    },
                )
                forked = apply_graph_blueprint_action(
                    controller,
                    {
                        "action": "fork",
                        "source_blueprint_id": "local_review",
                        "source_version": 1,
                        "blueprint_id": "local_review_copy",
                    },
                )
                revised = apply_graph_blueprint_action(
                    controller,
                    {
                        "action": "revise_envelope",
                        "source_blueprint_id": "local_review",
                        "source_version": 1,
                        "objective_class": "research",
                        "execution_profiles": "read_only",
                        "rationale": "Use the reusable draft for research-class objectives.",
                    },
                )
                snapshot = graph_control_snapshot(controller.state_path)
            finally:
                controller.close()

        self.assertIn("Blueprint Draft saved · local_review@1", "\n".join(created))
        self.assertIn("Blueprint fork saved · local_review_copy@1", "\n".join(forked))
        self.assertIn("Blueprint revision saved · local_review@2 · ACCEPTED", "\n".join(revised))
        self.assertIsNone(snapshot["selection"]["blueprint_id"])
        revisions = [
            item for item in snapshot["blueprints"]
            if item["blueprint_id"] == "local_review"
        ]
        self.assertEqual([item["version"] for item in revisions], [1, 2])
        self.assertEqual(revisions[1]["parent"], {"blueprint_id": "local_review", "version": 1})
        self.assertEqual(revisions[1]["revision_receipts"][0]["status"], "ACCEPTED")

    def test_graph_workbench_saves_and_revises_typed_multi_task_topology(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = build_parser().parse_args(
                [
                    "--config", str(root / "config.toml"),
                    "chat", "--workspace", str(FIXTURE_ROOT),
                    "--state", str(root / "runtime.db"),
                    "--provider", "openai-api", "--base-url", "http://127.0.0.1:9/v1",
                    "--model", "topology-workbench", "--no-auth", "--permission-mode", "read-only",
                ]
            )
            controller = ModernInteractiveController(
                args,
                {},
                provider_factory=lambda _config: self.fail("topology authoring must not build a provider"),
                coding_worker_factory=lambda _config: self.fail("topology authoring must not build a coding worker"),
                ports=_modern_controller_ports(),
            )
            topology = {
                "parameters": "objective, requested_outcome",
                "final_task_id": "final",
                "tasks": [
                    {
                        "task_id": "research",
                        "objective_template": "Research {{objective}}",
                        "depends_on": [],
                        "required_capabilities": ["analysis"],
                        "acceptance_templates": ["Evidence for {{requested_outcome}}"],
                        "risk_level": "LOW",
                    },
                    {
                        "task_id": "final",
                        "objective_template": "Integrate {{objective}}",
                        "depends_on": ["research"],
                        "required_capabilities": ["analysis"],
                        "acceptance_templates": ["Decision brief"],
                        "risk_level": "LOW",
                    },
                ],
            }
            try:
                saved = apply_graph_blueprint_action(
                    controller,
                    {
                        "action": "save_topology_draft",
                        "blueprint_id": "release_topology",
                        "objective_class": "general",
                        "execution_profiles": "read_only",
                        "topology": topology,
                    },
                )
                revised_topology = {
                    **topology,
                    "tasks": [
                        {**topology["tasks"][0], "objective_template": "Research release evidence for {{objective}}"},
                        topology["tasks"][1],
                    ],
                }
                revised = apply_graph_blueprint_action(
                    controller,
                    {
                        "action": "revise_topology",
                        "source_blueprint_id": "release_topology",
                        "source_version": 1,
                        "objective_class": "general",
                        "execution_profiles": "read_only",
                        "rationale": "Clarify the evidence boundary for the research task.",
                        "topology": revised_topology,
                    },
                )
                rejected = apply_graph_blueprint_action(
                    controller,
                    {
                        "action": "save_topology_draft",
                        "blueprint_id": "invalid_topology",
                        "objective_class": "general",
                        "execution_profiles": "read_only",
                        "topology": {
                            "parameters": "objective",
                            "final_task_id": "research",
                            "tasks": [{**topology["tasks"][0], "depends_on": ["missing"]}],
                        },
                    },
                )
                snapshot = graph_control_snapshot(controller.state_path)
            finally:
                controller.close()

        self.assertIn("Topology Blueprint Draft saved · release_topology@1", "\n".join(saved))
        self.assertIn("Topology Blueprint revision saved · release_topology@2 · ACCEPTED", "\n".join(revised))
        self.assertIn("Blueprint change was not saved", "\n".join(rejected))
        revisions = [item for item in snapshot["blueprints"] if item["blueprint_id"] == "release_topology"]
        self.assertEqual([item["task_count"] for item in revisions], [2, 2])
        self.assertEqual(revisions[1]["editor_tasks"][0]["depends_on"], ())
        self.assertEqual(revisions[1]["editor_tasks"][1]["depends_on"], ("research",))
        self.assertEqual(revisions[1]["revision_diff"]["source_version"], 1)
        self.assertEqual(revisions[1]["revision_diff"]["changed_tasks"][0]["fields"], ("objective",))
        self.assertNotIn("invalid_topology", [item["blueprint_id"] for item in snapshot["blueprints"]])

    def test_graph_workbench_accepts_the_full_64_task_gui_contract(self) -> None:
        """The GUI projection may page, but the typed authoring boundary is 64 tasks."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = build_parser().parse_args(
                [
                    "--config", str(root / "config.toml"),
                    "chat", "--workspace", str(FIXTURE_ROOT),
                    "--state", str(root / "runtime.db"),
                    "--provider", "openai-api", "--base-url", "http://127.0.0.1:9/v1",
                    "--model", "topology-64", "--no-auth", "--permission-mode", "read-only",
                ]
            )
            controller = ModernInteractiveController(
                args,
                {},
                provider_factory=lambda _config: self.fail("topology authoring must not build a provider"),
                coding_worker_factory=lambda _config: self.fail("topology authoring must not build a coding worker"),
                ports=_modern_controller_ports(),
            )
            tasks = [
                {
                    "task_id": f"task_{index:02d}",
                    "objective_template": f"Complete stage {index} for {{{{objective}}}}",
                    "depends_on": ([] if index == 1 else [f"task_{index - 1:02d}"]),
                    "required_capabilities": ["analysis"],
                    "acceptance_templates": [f"Evidence {index}"],
                    "risk_level": "LOW",
                }
                for index in range(1, 65)
            ]
            try:
                saved = apply_graph_blueprint_action(
                    controller,
                    {
                        "action": "save_topology_draft",
                        "blueprint_id": "full_topology",
                        "objective_class": "general",
                        "execution_profiles": "read_only",
                        "topology": {
                            "parameters": "objective, requested_outcome",
                            "final_task_id": "task_64",
                            "tasks": tasks,
                        },
                    },
                )
                snapshot = graph_control_snapshot(controller.state_path)
            finally:
                controller.close()

        self.assertIn("Topology Blueprint Draft saved · full_topology@1", "\n".join(saved))
        blueprint = next(item for item in snapshot["blueprints"] if item["blueprint_id"] == "full_topology")
        self.assertEqual(blueprint["task_count"], 64)
        self.assertEqual(len(blueprint["editor_tasks"]), 64)

    def test_knowledge_component_stays_provider_free_and_returns_tui_result(self) -> None:
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            remembered = execute_knowledge_command(
                state_path,
                "/remember",
                "application Knowledge boundary evidence",
            )
            retrieved = execute_knowledge_command(state_path, "/knowledge", "boundary")
            self.assertIsNotNone(remembered)
            self.assertIsNotNone(retrieved)
            self.assertIn("Remembered locally", "\n".join(remembered.messages))
            self.assertIn("application Knowledge boundary evidence", "\n".join(retrieved.messages))

    def test_job_audit_component_is_read_only_when_no_job_exists(self) -> None:
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "runtime.db"
            snapshot = job_audit_snapshot(state_path)
            catalog = job_audit_catalog(state_path)
            missing = job_audit_snapshot(state_path, "job-does-not-exist")
        self.assertEqual(snapshot["schema"], "noruct.job-audit-surface.v1")
        self.assertIsNone(snapshot["job"])
        self.assertEqual(catalog["schema"], "noruct.job-audit-catalog.v1")
        self.assertEqual(catalog["jobs"], ())
        self.assertIsNone(missing["job"])
        self.assertIn("matches", str(missing["error"]))
        self.assertTrue(execute_job_audit_command("").open_job_audit)
        specific = execute_job_audit_command("job-1")
        self.assertTrue(specific.open_job_audit)
        self.assertEqual(specific.job_audit_job_id, "job-1")
        self.assertIn("Usage", "\n".join(execute_job_audit_command("not valid").messages))

    def test_graph_change_summary_exposes_only_structural_terminal_facts(self) -> None:
        inspection = SimpleNamespace(
            final_graph_version=3,
            reconstructed_tasks=(
                {"task_id": "private-task-label", "status": "SUCCEEDED"},
                {"task_id": "another-private-label", "status": "CANCELLED"},
            ),
            execution_replica_groups=({"group_id": "private-group"},),
        )
        summary = _graph_change_summary(
            inspection,
            initial_digest="a" * 64,
            revisions=(
                {"operation": "INSERT", "budget_delta": 0.01, "next_digest": "b" * 64},
                {"operation": "CANCEL", "budget_delta": 0.0, "next_digest": "c" * 64},
            ),
        )

        self.assertEqual(summary["accepted_operations"], {"CANCEL": 1, "INSERT": 1})
        self.assertEqual(summary["final_task_status_counts"], {"CANCELLED": 1, "SUCCEEDED": 1})
        self.assertEqual(summary["final_task_count"], 2)
        self.assertEqual(summary["execution_replica_group_count"], 1)
        self.assertNotIn("private-task-label", str(summary))

    def test_frozen_budget_projection_whitelists_numeric_admission_limits(self) -> None:
        inspection = SimpleNamespace(
            job_limits={
                "max_tasks": 12,
                "max_concurrency": 3,
                "max_total_cost_usd": 1.25,
                "max_wall_time_ms": 45_000,
                "objective": "must never appear in a product audit",
                "unknown": {"nested": "not an envelope scalar"},
                "enabled": True,
                "max_total_tool_calls": -1,
            }
        )

        envelope = _frozen_budget_envelope(inspection)

        self.assertEqual(
            envelope,
            {
                "max_tasks": 12,
                "max_concurrency": 3,
                "max_total_cost_usd": 1.25,
                "max_wall_time_ms": 45_000,
            },
        )
        self.assertNotIn("objective", str(envelope))

    def test_frozen_route_admission_projection_is_verified_ordered_and_redacted(self) -> None:
        digest = "a" * 64

        class Admission:
            def __init__(self, route_id: str, uncertainty: float = 0.0) -> None:
                self.route_id = route_id
                self.binding = SimpleNamespace(
                    intelligence_snapshot_digest=digest,
                    compatibility_evidence_digest=digest,
                    egress_policy_digest=digest,
                    fallback_policy_digest=digest,
                )
                self.selection_receipt = SimpleNamespace(
                    selected_candidate=SimpleNamespace(uncertainty=uncertainty)
                )

            def operator_safe_summary(self) -> dict[str, object]:
                return {
                    "route_id": self.route_id,
                    "binding_digest": digest,
                    "selection_receipt_digest": digest,
                    "selection_policy_digest": digest,
                    "selection_reasons": ["POLICY_ORDER"],
                    # A product projection must whitelist the summary rather
                    # than accidentally exposing future unsafe fields.
                    "credential_reference": "not-for-display",
                    "requested_model_id": "not-for-display",
                    "provider_config_digest": "not-for-display",
                }

        class Store:
            def get_frozen_route_admission(self, run_id: str) -> object:
                if run_id == "tampered":
                    raise ValueError("durable record is invalid")
                return {
                    "first": Admission("route-first"),
                    "second": Admission("route-second"),
                    "legacy": None,
                    "nonfinite": Admission("route-nonfinite", float("nan")),
                    "out-of-range": Admission("route-out-of-range", 1.1),
                }[run_id]

        inspection = SimpleNamespace(
            runtime_runs=(
                SimpleNamespace(run_id="second", employee_id="employee-b", task_id="task-a"),
                SimpleNamespace(run_id="legacy", employee_id="employee-a", task_id="task-z"),
                SimpleNamespace(run_id="tampered", employee_id="employee-a", task_id="task-a"),
                SimpleNamespace(run_id="first", employee_id="employee-a", task_id="task-b"),
                SimpleNamespace(run_id="nonfinite", employee_id="employee-c", task_id="task-a"),
                SimpleNamespace(run_id="out-of-range", employee_id="employee-c", task_id="task-b"),
            )
        )

        projection = _frozen_route_admission_projection(Store(), inspection)  # type: ignore[arg-type]

        self.assertEqual(
            [(item["employee_id"], item["task_id"], item["route_id"]) for item in projection],
            [("employee-a", "task-b", "route-first"), ("employee-b", "task-a", "route-second")],
        )
        self.assertNotIn("run_id", str(projection))
        self.assertNotIn("credential_reference", str(projection))
        self.assertNotIn("requested_model_id", str(projection))
        self.assertNotIn("provider_config_digest", str(projection))
        self.assertEqual(projection[0]["selected_uncertainty"], 0.0)
        self.assertEqual(projection[0]["intelligence_snapshot_digest"], digest)
        self.assertEqual(projection[0]["compatibility_evidence_digest"], digest)
        self.assertEqual(projection[0]["egress_policy_digest"], digest)
        self.assertEqual(projection[0]["fallback_policy_digest"], digest)
        self.assertEqual(
            projection,
            _frozen_route_admission_projection(Store(), inspection),  # type: ignore[arg-type]
        )

    def test_model_invocation_projection_is_bound_ordered_and_content_free(self) -> None:
        binding_digest = "a" * 64

        class Admission:
            def operator_safe_summary(self) -> dict[str, object]:
                return {
                    "route_id": "local-review-route",
                    "binding_digest": binding_digest,
                    "selection_receipt_digest": "b" * 64,
                    "selection_policy_digest": "c" * 64,
                }

        def receipt(
            invocation_id: str,
            *,
            cost_availability: str,
            cost_usd: float | None,
            route_binding_digest: str = binding_digest,
        ) -> ModelInvocationReceipt:
            return ModelInvocationReceipt(
                invocation_id=invocation_id,
                route_binding_digest=route_binding_digest,
                context_projection_digest="d" * 64,
                attempt_id="attempt-private",
                fanout_parent_id="fanout-private",
                terminal_status="SUCCEEDED",
                output_digest="e" * 64,
                usage_availability="UNAVAILABLE",
                usage_units=None,
                cost_availability=cost_availability,
                cost_usd=cost_usd,
                latency_ms=0.0,
            )

        zero_cost = receipt("call-zero", cost_availability="AVAILABLE", cost_usd=0.0)
        unknown_cost = receipt("call-unknown", cost_availability="UNAVAILABLE", cost_usd=None)
        foreign = receipt(
            "call-foreign",
            cost_availability="AVAILABLE",
            cost_usd=1.0,
            route_binding_digest="f" * 64,
        )

        class Store:
            def get_frozen_route_admission(self, run_id: str) -> object:
                if run_id == "tampered":
                    raise ValueError("admission is invalid")
                return Admission()

            def list_model_invocation_receipts(self, run_id: str) -> list[ModelInvocationReceipt]:
                if run_id == "missing":
                    raise KeyError("run is missing")
                return [unknown_cost, foreign, zero_cost]

        inspection = SimpleNamespace(
            runtime_runs=(
                SimpleNamespace(run_id="missing", employee_id="employee-a", task_id="task-b"),
                SimpleNamespace(run_id="tampered", employee_id="employee-a", task_id="task-a"),
                SimpleNamespace(run_id="valid", employee_id="employee-a", task_id="task-a"),
            )
        )

        projection = _model_invocation_receipt_projection(Store(), inspection)  # type: ignore[arg-type]

        self.assertEqual(len(projection), 2)
        self.assertEqual(
            [item["receipt_digest"] for item in projection],
            sorted((zero_cost.digest, unknown_cost.digest)),
        )
        by_availability = {item["cost_availability"]: item for item in projection}
        self.assertEqual(by_availability["AVAILABLE"]["cost_usd"], 0.0)
        self.assertIsNone(by_availability["UNAVAILABLE"]["cost_usd"])
        self.assertEqual(by_availability["AVAILABLE"]["latency_ms"], 0.0)
        self.assertEqual(set(projection[0]), {
            "employee_id", "task_id", "route_id", "binding_digest", "receipt_digest",
            "terminal_status", "usage_availability", "cost_availability", "cost_usd", "latency_ms",
        })
        rendered = str(projection)
        self.assertNotIn("call-zero", rendered)
        self.assertNotIn("attempt-private", rendered)
        self.assertNotIn("fanout-private", rendered)
        self.assertNotIn("context_projection", rendered)
        self.assertNotIn("output_digest", rendered)

    def test_observed_execution_avoids_a_causal_graph_impact_claim(self) -> None:
        inspection = SimpleNamespace(
            job_status="SUCCEEDED",
            reconstructed_tasks=(
                {"task_id": "private-task-label", "status": "SUCCEEDED"},
                {"task_id": "another-private-label", "status": "FAILED"},
            ),
            validation_receipts=(
                {"name": "private validation name", "status": "PASSED"},
            ),
            tool_receipts=(
                {"tool_name": "private", "effect": "WRITE", "status": "COMPLETED"},
                {"tool_name": "private", "effect": "READ", "status": "COMPLETED"},
            ),
        )

        observed = _observed_execution(inspection)

        self.assertEqual(observed["terminal_status"], "SUCCEEDED")
        self.assertEqual(observed["task_status_counts"], {"FAILED": 1, "SUCCEEDED": 1})
        self.assertEqual(observed["coding_validation_status_counts"], {"PASSED": 1})
        self.assertEqual(observed["effect_receipt_status"], "PARTIAL")
        self.assertEqual(observed["work_outcome_status"], "NOT_VERIFIED")
        self.assertNotIn("private-task-label", str(observed))
        self.assertNotIn("private validation name", str(observed))

    def test_read_only_continuation_hint_is_not_authority_and_excludes_unsafe_runs(self) -> None:
        safe = SimpleNamespace(
            audit_status=SimpleNamespace(value="INTERRUPTED"),
            replay_matches=True,
            requested_effect="READ",
            mutation_count=0,
            graph_patch_count=0,
            graph_proposal_decisions=(),
            reconstructed_tasks=(
                {"task_id": "analysis", "status": "SUCCEEDED"},
                {"task_id": "final", "status": "PENDING"},
            ),
            runtime_runs=(SimpleNamespace(status="SUCCEEDED"),),
        )
        unsafe = SimpleNamespace(
            **{
                **safe.__dict__,
                "runtime_runs": (SimpleNamespace(status="RUNNING"),),
            }
        )

        projection = _read_only_continuation_candidate(safe)
        self.assertTrue(projection["candidate"])
        self.assertEqual(projection["successful_task_count"], 1)
        self.assertIn("one_shot_receipt_claim", projection["requires"])
        self.assertFalse(_read_only_continuation_candidate(unsafe)["candidate"])

    def test_runtime_settings_component_delegates_authority_change_to_host(self) -> None:
        class Host:
            config = SimpleNamespace(permission_mode="read-only")

            def _persist_global_runtime_defaults(self, **changes: object) -> None:
                for key, value in changes.items():
                    setattr(self.config, key, value)

        host = Host()
        result = execute_runtime_settings_command(host, "/permission", "ask")
        self.assertIsNotNone(result)
        self.assertEqual(host.config.permission_mode, "ask")
        self.assertIn("workspace tools", "\n".join(result.messages))

    def test_routing_policy_terminal_command_persists_only_future_job_preference(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = build_parser().parse_args(
                [
                    "--config", str(root / "config.toml"),
                    "chat", "--workspace", str(FIXTURE_ROOT),
                    "--state", str(root / "runtime.db"),
                    "--provider", "openai-api", "--base-url", "http://127.0.0.1:9/v1",
                    "--model", "routing-policy", "--no-auth", "--permission-mode", "read-only",
                ]
            )
            controller = ModernInteractiveController(
                args,
                {},
                provider_factory=lambda _config: self.fail("routing policy must not build a provider"),
                coding_worker_factory=lambda _config: self.fail("routing policy must not build a coding worker"),
                ports=_modern_controller_ports(),
            )
            try:
                saved = asyncio.run(controller.execute_command("/routing-policy QUALITY_FIRST"))
                current = asyncio.run(controller.execute_command("/routing-policy"))
                help_result = asyncio.run(controller.execute_command("/help"))
                persisted = load_local_routing_settings(root / "config.toml")
                public_text = "\n".join(saved.messages + current.messages)
                self.assertEqual(persisted.policy.mode, UserRoutingPolicyMode.QUALITY_FIRST)
                self.assertEqual(persisted.approved_routes.routes, ())
                self.assertIn("future Jobs only", public_text)
                self.assertIn("does not activate routes, credentials, or egress", public_text)
                self.assertNotIn("openai", public_text.lower())
                self.assertIn("/routing-policy [QUALITY_FIRST|BALANCED|EFFICIENT|PRIVATE_LOCAL_FIRST]", "\n".join(help_result.messages))
                self.assertEqual(controller.turn_count, 0)
            finally:
                controller.close()

    def test_routing_policy_validation_preserves_approved_registry_and_never_writes_invalid_input(self) -> None:
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            registry = ApprovedRouteRegistry((
                ApprovedRouteMetadata(
                    route_id="approved-route",
                    execution_route_binding_digest="b" * 64,
                    provider_config_digest="a" * 64,
                    credential_reference="ROUTE_CREDENTIAL_REF",
                ),
            ))
            write_local_routing_settings(
                config_path,
                LocalRoutingSettings(
                    policy=UserRoutingPolicy(UserRoutingPolicyMode.BALANCED),
                    approved_routes=registry,
                ),
            )

            class Host:
                config = SimpleNamespace(config_path=config_path)

            before = config_path.read_bytes()
            no_argument = execute_runtime_settings_command(Host(), "/routing-policy", "")
            invalid = execute_runtime_settings_command(Host(), "/routing-policy", "QUALITY_FIRST extra")
            unknown = execute_runtime_settings_command(Host(), "/routing-policy", "unknown")
            self.assertEqual(config_path.read_bytes(), before)
            self.assertIn("BALANCED", "\n".join(no_argument.messages))
            self.assertIn("Usage", "\n".join(invalid.messages))
            self.assertIn("must be one of", "\n".join(unknown.messages))

            saved = execute_runtime_settings_command(Host(), "/routing-policy", "EFFICIENT")
            persisted = load_local_routing_settings(config_path)
            self.assertEqual(persisted.policy.mode, UserRoutingPolicyMode.EFFICIENT)
            self.assertEqual(persisted.approved_routes.canonical_json(), registry.canonical_json())
            public_text = "\n".join(saved.messages)
            self.assertNotIn("approved-route", public_text)
            self.assertNotIn("ROUTE_CREDENTIAL_REF", public_text)

    def test_synthetic_route_onboarding_requires_confirm_and_never_activates_provider(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = build_parser().parse_args(
                [
                    "--config", str(root / "config.toml"),
                    "chat", "--workspace", str(FIXTURE_ROOT),
                    "--state", str(root / "runtime.db"),
                    "--provider", "openai-api", "--base-url", "http://127.0.0.1:9/v1",
                    "--model", "routing-onboarding", "--no-auth", "--permission-mode", "read-only",
                ]
            )
            controller = ModernInteractiveController(
                args,
                {},
                provider_factory=lambda _config: self.fail("route onboarding must not build a provider"),
                coding_worker_factory=lambda _config: self.fail("route onboarding must not build a coding worker"),
                ports=_modern_controller_ports(),
            )
            payload = {
                "fixture": "SYNTHETIC_PROVIDER_FREE",
                "route_id": "synthetic-local-route",
                "execution_route_binding_digest": "a" * 64,
                "provider_config_digest": "b" * 64,
                "credential_reference": "SYNTHETIC_ROUTE_REFERENCE",
            }
            command_payload = json.dumps(payload, sort_keys=True)
            try:
                initial = root / "config.toml"
                before = initial.read_bytes() if initial.exists() else b""
                preview = asyncio.run(
                    controller.execute_command("/routing-onboard preview " + command_payload)
                )
                self.assertEqual(initial.read_bytes() if initial.exists() else b"", before)
                self.assertIn("ready for explicit local confirmation", "\n".join(preview.messages))

                confirmed = asyncio.run(
                    controller.execute_command("/routing-onboard confirm " + command_payload)
                )
                persisted = load_local_routing_settings(initial)
                self.assertEqual(
                    [item.route_id for item in persisted.approved_routes.routes],
                    ["synthetic-local-route"],
                )
                self.assertIn("activates no provider or egress", "\n".join(confirmed.messages))
                self.assertNotIn("SYNTHETIC_ROUTE_REFERENCE", "\n".join(confirmed.messages))

                asyncio.run(controller.execute_command("/routing-policy QUALITY_FIRST"))
                asyncio.run(controller.execute_command("/routing-policy EFFICIENT"))
                asyncio.run(controller.execute_command("/routing-policy QUALITY_FIRST"))
                rolled_back = load_local_routing_settings(initial)
                self.assertEqual(rolled_back.policy.mode, UserRoutingPolicyMode.QUALITY_FIRST)
                self.assertEqual(
                    rolled_back.approved_routes.canonical_json(),
                    persisted.approved_routes.canonical_json(),
                )

                bytes_after_confirm = initial.read_bytes()
                repeated = asyncio.run(
                    controller.execute_command("/routing-onboard confirm " + command_payload)
                )
                self.assertEqual(initial.read_bytes(), bytes_after_confirm)
                self.assertIn("needs no repeated confirmation", "\n".join(repeated.messages))

                conflict = {**payload, "provider_config_digest": "c" * 64}
                rejected = asyncio.run(
                    controller.execute_command(
                        "/routing-onboard confirm " + json.dumps(conflict, sort_keys=True)
                    )
                )
                self.assertEqual(initial.read_bytes(), bytes_after_confirm)
                self.assertIn("was refused", "\n".join(rejected.messages))
                self.assertEqual(controller.turn_count, 0)
            finally:
                controller.close()


if __name__ == "__main__":
    unittest.main()
