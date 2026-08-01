"""Static ownership checks for deliberately one-way component boundaries."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


_ROOT = Path(__file__).parents[1]


def _imports_from(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


class ComponentImportBoundaryTests(unittest.TestCase):
    def test_first_party_implementation_files_stay_within_the_composition_budget(self) -> None:
        component_files = [
            path
            for path in (_ROOT / "src/dynamic_firm").rglob("*.py")
            if not {"_vendor", "vendored_sources"}.intersection(path.parts)
        ]
        oversized = {
            path.relative_to(_ROOT / "src/dynamic_firm").as_posix(): len(
                path.read_text(encoding="utf-8").splitlines()
            )
            for path in component_files
            if len(path.read_text(encoding="utf-8").splitlines()) > 1_000
        }
        self.assertEqual(oversized, {})

    def test_goal_runtime_stages_use_explicit_ports_not_module_namespace_mutation(
        self,
    ) -> None:
        for name in (
            "goal_runtime.py",
            "goal_capability_runtime.py",
            "goal_planning_runtime.py",
            "goal_completion_runtime.py",
        ):
            source = (_ROOT / "src/dynamic_firm/application" / name).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("__dict__.update", source, name)
            self.assertNotIn("globals().update", source, name)

    def test_company_application_ingress_cannot_construct_work_orders_directly(
        self,
    ) -> None:
        """User-facing ingress must pass the canonical Front Door normalizer."""

        direct_calls: list[str] = []
        for path in sorted((_ROOT / "src/dynamic_firm/application").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if (
                    isinstance(function, ast.Name)
                    and function.id == "WorkOrder"
                ) or (
                    isinstance(function, ast.Attribute)
                    and function.attr == "WorkOrder"
                ):
                    direct_calls.append(
                        f"{path.relative_to(_ROOT).as_posix()}:{node.lineno}"
                    )
        self.assertEqual(direct_calls, [])

    def test_work_order_constructor_is_owned_only_by_normalization_and_decoder(
        self,
    ) -> None:
        allowed = {
            "frontdoor.py",
            "work_order_portfolio_models.py",
        }
        violations: list[str] = []
        company_root = _ROOT / "src/dynamic_firm/company"
        for path in sorted(company_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                is_constructor = (
                    isinstance(function, ast.Name)
                    and function.id == "WorkOrder"
                ) or (
                    isinstance(function, ast.Attribute)
                    and function.attr == "WorkOrder"
                )
                if is_constructor and path.name not in allowed:
                    violations.append(
                        f"{path.relative_to(_ROOT).as_posix()}:{node.lineno}"
                    )
        self.assertEqual(violations, [])

    def test_cli_facade_uses_the_declared_component_contract(self) -> None:
        facade = (_ROOT / "src/dynamic_firm/cli.py").read_text(encoding="utf-8")
        contract = (
            _ROOT / "src/dynamic_firm/application/cli_component_contract.py"
        ).read_text(encoding="utf-8")
        self.assertIn("cli_component_contract", facade)
        self.assertIn("_COMPONENT_OWNER", contract)
        for source in (facade, contract):
            self.assertNotIn("globals().update", source)
            self.assertNotIn("__dict__.update", source)

    def test_session_recall_runtime_consumes_a_port_not_product_store(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/runtime/company_session_recall.py"
        )
        self.assertFalse(
            any(name.startswith("dynamic_firm.product") for name in imports),
            imports,
        )

    def test_company_community_grammar_does_not_depend_on_evolution_transport(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/company/community_blueprints.py"
        )
        self.assertFalse(
            any(name.startswith("dynamic_firm.evolution") for name in imports),
            imports,
        )

    def test_community_parser_only_declares_cli_schema(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/application/graph_community_cli_parser.py"
        )
        self.assertEqual(imports, {"__future__", "argparse", "pathlib"})

    def test_foundation_evidence_parser_only_declares_cli_schema(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/application/foundation_cli/evidence_parser.py"
        )
        self.assertEqual(imports, {"__future__", "argparse", "pathlib"})

    def test_foundation_core_parser_only_declares_cli_schema(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/application/foundation_cli/core_parser.py"
        )
        self.assertEqual(imports, {"__future__", "argparse", "pathlib"})

    def test_foundation_dispatch_does_not_depend_on_global_cli_ingress(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/application/foundation_cli/command.py"
        )
        self.assertNotIn("dynamic_firm.cli", imports)

    def test_skills_parser_only_declares_cli_schema(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/application/skills_cli_parser.py"
        )
        self.assertEqual(imports, {"__future__", "argparse", "pathlib"})

    def test_schedule_parser_only_declares_cli_schema(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/application/schedule_cli_parser.py"
        )
        self.assertEqual(imports, {"__future__", "argparse", "pathlib"})

    def test_gateway_parser_only_declares_cli_schema(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/application/gateway_cli_parser.py"
        )
        self.assertEqual(imports, {"__future__", "argparse", "pathlib"})

    def test_session_parser_only_declares_cli_schema(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/application/session_cli_parser.py"
        )
        self.assertEqual(imports, {"__future__", "argparse", "pathlib"})

    def test_skills_adapters_do_not_depend_on_global_cli_ingress(self) -> None:
        for name in (
            "skills_cli.py",
            "managed_skills_cli.py",
            "schedule_cli.py",
            "knowledge_intent_cli_parser.py",
            "company_cli_parser.py",
            "evolution_cli_parser.py",
            "evaluation_cli_parser.py",
            "evaluation_workflow_cli_parser.py",
            "integration_cli_parser.py",
            "operator_control_cli_parser.py",
            "runtime_control_cli_parser.py",
            "evolution_cli.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/application" / name)
            self.assertNotIn("dynamic_firm.cli", imports, name)

    def test_knowledge_cli_parser_renderer_and_orchestration_stay_one_way(self) -> None:
        """Keep schema, rendering, and stateful Knowledge work independently testable."""

        parser_imports = _imports_from(
            _ROOT / "src/dynamic_firm/application/knowledge_intent_cli_parser.py"
        )
        self.assertEqual(
            parser_imports,
            {"__future__", "argparse", "pathlib", "typing", "dynamic_firm.knowledge"},
        )

        renderer_imports = _imports_from(
            _ROOT / "src/dynamic_firm/application/knowledge_cli_renderer.py"
        )
        self.assertEqual(
            renderer_imports,
            {"__future__", "typing", "dynamic_firm.application.cli_component_contract"},
        )

        command_imports = _imports_from(
            _ROOT / "src/dynamic_firm/application/knowledge_cli.py"
        )
        self.assertNotIn("dynamic_firm.cli", command_imports)
        self.assertNotIn("dynamic_firm.kernel", command_imports)
        self.assertFalse(
            any(name.startswith("dynamic_firm.runtime") for name in command_imports),
            command_imports,
        )

        workbench_imports = _imports_from(
            _ROOT / "src/dynamic_firm/product/knowledge_commands.py"
        )
        self.assertNotIn("dynamic_firm.cli", workbench_imports)
        self.assertFalse(
            any(name.startswith("dynamic_firm.application") for name in workbench_imports),
            workbench_imports,
        )
        self.assertFalse(
            any(name.startswith("dynamic_firm.kernel") for name in workbench_imports),
            workbench_imports,
        )

    def test_continuation_preflight_components_do_not_gain_cli_or_provider_authority(
        self,
    ) -> None:
        for name in (
            "continuation_runtime_preflight.py",
            "continuation_artifact_preflight.py",
            "continuation_capability_assembly.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/application" / name)
            self.assertNotIn("dynamic_firm.cli", imports, name)
            self.assertFalse(
                any(name.startswith("dynamic_firm.providers") for name in imports),
                (name, imports),
            )
            self.assertNotIn("dynamic_firm.kernel.service", imports, name)

    def test_modern_operator_snapshot_assembly_does_not_gain_cli_or_provider_authority(
        self,
    ) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/application/modern_terminal_operator_snapshot.py"
        )
        self.assertNotIn("dynamic_firm.cli", imports)
        self.assertFalse(
            any(name.startswith("dynamic_firm.providers") for name in imports),
            imports,
        )

    def test_shared_operator_read_model_does_not_gain_cli_or_provider_authority(
        self,
    ) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/application/operator_surface_read_model.py"
        )
        self.assertNotIn("dynamic_firm.cli", imports)
        self.assertFalse(
            any(name.startswith("dynamic_firm.providers") for name in imports),
            imports,
        )
        self.assertNotIn("dynamic_firm.kernel.service", imports)

    def test_store_mixins_do_not_depend_on_their_owner_module(self) -> None:
        for name in (
            "store_tool_approval.py",
            "store_effect_recovery.py",
            "store_remote_effect_coordination.py",
            "store_job_audit.py",
            "store_graph_proposal_continuation.py",
            "store_job_outcome.py",
            "store_job_lifecycle.py",
            "store_company_budget_lifecycle.py",
            "store_run_lifecycle.py",
            "store_schema.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/runtime" / name)
            self.assertNotIn("dynamic_firm.runtime.store", imports, name)

    def test_company_store_mixins_do_not_depend_on_owner_module(self) -> None:
        for name in (
            "store_staffing_demand.py",
            "store_employee_skill_catalog.py",
            "store_employee_skill_lifecycle.py",
            "store_employee_skill_observation.py",
            "store_roster_patch.py",
            "store_hire_observation.py",
            "store_workflow_patch.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/company" / name)
            self.assertNotIn("dynamic_firm.company.store", imports, name)

    def test_knowledge_store_mixins_do_not_depend_on_owner_module(self) -> None:
        for name in (
            "store_core_lifecycle.py",
            "store_retrieval.py",
            "store_evidence_execution.py",
            "store_intent_decision.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/knowledge" / name)
            self.assertNotIn("dynamic_firm.knowledge.store", imports, name)

    def test_knowledge_archive_does_not_depend_on_product_or_cli(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/knowledge/lifecycle_archive.py"
        )
        self.assertNotIn("dynamic_firm.cli", imports)
        self.assertFalse(
            any(name.startswith("dynamic_firm.product") for name in imports),
            imports,
        )

    def test_cross_plane_attention_is_read_only_and_does_not_open_owner_stores(self) -> None:
        path = _ROOT / "src/dynamic_firm/product/cross_plane_attention.py"
        imports = _imports_from(path)
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("dynamic_firm.evolution.store", imports)
        self.assertNotIn("dynamic_firm.evolution.service", imports)
        self.assertNotIn("dynamic_firm.runtime.store", imports)
        for owner in (
            "KnowledgeStore",
            "EvolutionStore",
            "EvolutionNetworkService",
            "RunStore",
        ):
            self.assertNotIn(owner, source, owner)

    def test_plugin_registry_helpers_remain_outside_runtime_and_cli_authority(self) -> None:
        for name in ("plugin_registry.py", "plugin_lifecycle_receipts.py"):
            imports = _imports_from(_ROOT / "src/dynamic_firm/product" / name)
            self.assertNotIn("dynamic_firm.cli", imports, name)
            self.assertFalse(
                any(
                    item.startswith("dynamic_firm.runtime")
                    or item.startswith("dynamic_firm.application")
                    for item in imports
                ),
                (name, imports),
            )

    def test_plugin_protocol_remains_outside_cli_and_company_authority(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/product/executable_plugin_protocol.py"
        )
        self.assertNotIn("dynamic_firm.cli", imports)
        self.assertFalse(
            any(
                item.startswith("dynamic_firm.application")
                or item.startswith("dynamic_firm.company")
                for item in imports
            ),
            imports,
        )

    def test_mcp_schema_contract_remains_outside_cli_provider_and_company_authority(
        self,
    ) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/mcp_schema_contract.py"
        )
        self.assertNotIn("dynamic_firm.cli", imports)
        self.assertFalse(
            any(
                item.startswith("dynamic_firm.application")
                or item.startswith("dynamic_firm.providers")
                or item.startswith("dynamic_firm.company")
                for item in imports
            ),
            imports,
        )

    def test_kernel_components_do_not_depend_on_service_owner(self) -> None:
        for name in (
            "primitives.py",
            "mutation_execution.py",
            "policy_request.py",
            "result_supervision.py",
            "ingress.py",
            "managed_continuation.py",
            "managed_terminal.py",
            "managed_completion.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/kernel" / name)
            self.assertNotIn("dynamic_firm.kernel.service", imports, name)

    def test_dynamic_workflow_compiler_does_not_depend_on_cli_or_state_store(
        self,
    ) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/compiler/dynamic_workflow_compiler.py"
        )
        self.assertNotIn("dynamic_firm.cli", imports)
        self.assertNotIn("dynamic_firm.company.store", imports)

    def test_active_job_primitives_do_not_depend_on_ledger_owner(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/runtime/job_ledger_primitives.py"
        )
        self.assertNotIn("dynamic_firm.runtime.job_ledger", imports)

    def test_active_job_components_do_not_depend_on_ledger_facade(self) -> None:
        for name in (
            "job_ledger_writer.py",
            "job_inspector_recovery.py",
            "job_inspector_checkpoints.py",
            "job_inspector.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/runtime" / name)
            self.assertNotIn("dynamic_firm.runtime.job_ledger", imports, name)

    def test_employee_loop_components_do_not_depend_on_loop_owner(self) -> None:
        for name in (
            "loop_session.py",
            "loop_approval.py",
            "loop_outcome.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/runtime" / name)
            self.assertNotIn("dynamic_firm.runtime.loop", imports, name)

    def test_tool_components_do_not_depend_on_tools_facade(self) -> None:
        for name in (
            "tool_contracts.py",
            "tool_executor.py",
            "workspace_read_tools.py",
            "workspace_mutation_tools.py",
            "workspace_background_tools.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/runtime" / name)
            self.assertNotIn("dynamic_firm.runtime.tools", imports, name)

    def test_evolution_store_components_do_not_depend_on_store_owner(self) -> None:
        for name in (
            "store_primitives.py",
            "store_schema.py",
            "store_consents.py",
            "store_release_registry.py",
            "store_blueprints.py",
            "store_artifact_network.py",
            "store_artifact_registry.py",
            "store_artifact_regression.py",
            "store_artifact_shadow.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/evolution" / name)
            self.assertNotIn("dynamic_firm.evolution.store", imports, name)

    def test_artifact_lifecycle_does_not_bypass_cli_or_store_authority(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/evolution/artifact_lifecycle.py"
        )
        self.assertNotIn("dynamic_firm.cli", imports)
        self.assertNotIn("dynamic_firm.evolution.store", imports)

    def test_exact_context_pair_contracts_do_not_depend_on_owner_module(self) -> None:
        for name in (
            "exact_context_live_pair_contracts.py",
            "exact_context_live_pair_primitives.py",
            "exact_context_live_pair_preparation.py",
            "exact_context_live_pair_execution.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/evaluation" / name)
            self.assertNotIn(
                "dynamic_firm.evaluation.exact_context_live_pair",
                imports,
                name,
            )

    def test_workflow_patch_efficiency_components_do_not_depend_on_owner_module(
        self,
    ) -> None:
        for name in (
            "workflow_patch_efficiency_contracts.py",
            "workflow_patch_efficiency_primitives.py",
            "workflow_patch_efficiency_preparation.py",
            "workflow_patch_efficiency_status.py",
            "workflow_patch_efficiency_natural.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/evaluation" / name)
            self.assertNotIn(
                "dynamic_firm.evaluation.workflow_patch_efficiency",
                imports,
                name,
            )

    def test_workflow_patch_extension_components_do_not_depend_on_owner_module(
        self,
    ) -> None:
        for name in (
            "workflow_patch_extension_contracts.py",
            "workflow_patch_extension_primitives.py",
            "workflow_patch_extension_preparation.py",
            "workflow_patch_extension_status.py",
            "workflow_patch_extension_execution.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/evaluation" / name)
            self.assertNotIn(
                "dynamic_firm.evaluation.workflow_patch_extension",
                imports,
                name,
            )

    def test_workflow_patch_campaign_components_do_not_depend_on_owner_module(
        self,
    ) -> None:
        for name in (
            "workflow_patch_campaign_contracts.py",
            "workflow_patch_campaign_primitives.py",
            "workflow_patch_campaign_preparation.py",
            "workflow_patch_campaign_status.py",
            "workflow_patch_campaign_execution.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/evaluation" / name)
            self.assertNotIn(
                "dynamic_firm.evaluation.workflow_patch_campaign",
                imports,
                name,
            )

    def test_firm_value_campaign_source_does_not_depend_on_campaign_owner(
        self,
    ) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/evaluation/firm_value_campaign_source.py"
        )
        self.assertNotIn(
            "dynamic_firm.evaluation.firm_value_campaign",
            imports,
        )

    def test_connectivity_cli_does_not_depend_on_global_cli_ingress(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/application/connectivity_cli.py"
        )
        self.assertNotIn("dynamic_firm.cli", imports)

    def test_evaluation_core_cli_does_not_depend_on_global_cli_ingress(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/application/evaluation_core_cli.py"
        )
        self.assertNotIn("dynamic_firm.cli", imports)

    def test_evaluation_command_adapters_do_not_depend_on_global_cli_ingress(
        self,
    ) -> None:
        for name in (
            "evaluation_workflow_cli.py",
            "evaluation_firm_cli.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/application" / name)
            self.assertNotIn("dynamic_firm.cli", imports, name)

    def test_mcp_cli_does_not_depend_on_global_cli_ingress(self) -> None:
        imports = _imports_from(_ROOT / "src/dynamic_firm/application/mcp_cli.py")
        self.assertNotIn("dynamic_firm.cli", imports)

    def test_plugin_cli_does_not_depend_on_global_cli_ingress(self) -> None:
        imports = _imports_from(_ROOT / "src/dynamic_firm/application/plugin_cli.py")
        self.assertNotIn("dynamic_firm.cli", imports)

    def test_tui_components_do_not_depend_on_the_compatibility_facade(self) -> None:
        for name in (
            "tui_constants.py",
            "tui_primitives.py",
            "tui_interactions.py",
            "tui_inline.py",
            "tui_live_dock.py",
            "tui_live.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/product" / name)
            self.assertNotIn("dynamic_firm.product.tui", imports, name)

    def test_modern_tui_components_do_not_depend_on_the_compatibility_facade(
        self,
    ) -> None:
        for name in ("modern_tui_contracts.py", "modern_tui_app.py"):
            imports = _imports_from(_ROOT / "src/dynamic_firm/product" / name)
            self.assertNotIn("dynamic_firm.product.modern_tui", imports, name)

    def test_settings_screen_components_do_not_depend_on_the_screen_owner(
        self,
    ) -> None:
        for name in (
            "modern_tui_settings_compose.py",
            "modern_tui_settings_actions.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm/product" / name)
            self.assertNotIn(
                "dynamic_firm.product.modern_tui_settings_screen",
                imports,
                name,
            )

    def test_graph_modal_does_not_depend_on_secondary_modal_owner(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/product/modern_tui_graph_screen.py"
        )
        self.assertNotIn(
            "dynamic_firm.product.modern_tui_secondary_screens",
            imports,
        )

    def test_session_transcript_does_not_depend_on_session_store_owner(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/product/session_transcript.py"
        )
        self.assertNotIn("dynamic_firm.product.sessions", imports)

    def test_employee_worker_shims_do_not_depend_on_worker_entrypoint(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/foundation/employee_worker_shims.py"
        )
        self.assertNotIn("dynamic_firm.foundation._employee_worker", imports)

    def test_runtime_parity_component_does_not_depend_on_product_facade(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/foundation/runtime_parity.py"
        )
        self.assertNotIn("dynamic_firm.foundation.parity", imports)

    def test_codex_coding_worker_does_not_depend_on_cli_or_runtime_store(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/providers/codex_coding_worker.py"
        )
        self.assertNotIn("dynamic_firm.cli", imports)
        self.assertNotIn("dynamic_firm.runtime.store", imports)

    def test_mcp_components_do_not_depend_on_cli_ingress(self) -> None:
        for name in (
            "mcp_action_connector.py",
            "mcp_read_policy.py",
            "mcp_read_group.py",
        ):
            imports = _imports_from(_ROOT / "src/dynamic_firm" / name)
            self.assertNotIn("dynamic_firm.cli", imports, name)

    def test_company_domain_models_do_not_depend_on_state_store(self) -> None:
        for name in ("roster_models.py", "workflow_models.py"):
            imports = _imports_from(_ROOT / "src/dynamic_firm/company" / name)
            self.assertNotIn("dynamic_firm.company.store", imports, name)

    def test_runtime_execution_components_do_not_depend_on_cli(self) -> None:
        for name in ("runtime_execution.py", "worker_transport.py"):
            imports = _imports_from(_ROOT / "src/dynamic_firm/foundation" / name)
            self.assertNotIn("dynamic_firm.cli", imports, name)

    def test_campaign_v2_execution_does_not_depend_on_cli(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/evaluation/firm_value_campaign_v2_execution.py"
        )
        self.assertNotIn("dynamic_firm.cli", imports)

    def test_release_authorization_benchmark_does_not_depend_on_cli(self) -> None:
        imports = _imports_from(
            _ROOT
            / "src/dynamic_firm/evaluation/release_authorization_benchmark.py"
        )
        self.assertNotIn("dynamic_firm.cli", imports)

    def test_information_boundary_live_does_not_depend_on_cli(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/evaluation/information_boundary_live.py"
        )
        self.assertNotIn("dynamic_firm.cli", imports)

    def test_closed_loop_materialized_engine_does_not_depend_on_cli(self) -> None:
        imports = _imports_from(
            _ROOT / "src/dynamic_firm/evaluation/closed_loop_materialized.py"
        )
        self.assertNotIn("dynamic_firm.cli", imports)

    def test_foundation_parity_uses_evaluation_product_contract(self) -> None:
        imports = _imports_from(_ROOT / "src/dynamic_firm/foundation/parity.py")
        self.assertNotIn("dynamic_firm.cli", imports)
        self.assertFalse(
            any(name.startswith("dynamic_firm.product") for name in imports),
            imports,
        )
        self.assertIn(
            "dynamic_firm.evaluation.product_preview_contract",
            imports,
        )
