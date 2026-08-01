from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
import shutil
import sys

from dynamic_firm.application.cli_composition import RunCommandConfig
from dynamic_firm.application.continuation_capability_assembly import (
    assemble_continuation_capabilities,
)
from dynamic_firm.application.continuation_runtime_preflight import (
    ContinuationRuntimePreflightCode,
    ContinuationRuntimePreflightError,
    granted_tool_contract_digest,
)
from dynamic_firm.company.graph_blueprint_models import (
    GraphBlueprint,
    GraphBlueprintOrigin,
    GraphBlueprintTask,
)
from dynamic_firm.company.graph_blueprint_registry import SQLiteGraphBlueprintRegistry
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.product.external_skills import (
    ExternalSkillPackageTools,
    discover_external_skills,
    select_external_skills,
)
from dynamic_firm.product.mcp_settings import remove_mcp_settings, write_mcp_settings
from dynamic_firm.mcp_connector import McpReadOnlyConfig
from dynamic_firm.runtime.models import (
    ActionPolicy,
    RunLimits,
    ToolEffect,
    ToolGrant,
)
from tests.kernel.helpers import company_request, task


_ROSTER = (EmployeeRecord("employee", "Analyst", ("analysis",)),)


class ContinuationCapabilityAssemblyTests(unittest.TestCase):
    def _config(
        self,
        root: Path,
        *,
        skill_dirs: tuple[Path, ...] = (),
        mcp_read_only=None,
    ) -> RunCommandConfig:
        return RunCommandConfig(
            goal="review repository acceptance",
            workspace=root,
            state_path=root / "runtime.db",
            provider_kind="openai_api",
            base_url="https://unused.invalid/v1",
            model="fixture",
            codex_model=None,
            codex_command="codex",
            api_key_env=None,
            request_timeout_seconds=5.0,
            permission_mode="read-only",
            run_limits=RunLimits(),
            mcp_read_only=mcp_read_only,
            external_skill_dirs=skill_dirs,
            config_path=root / "config.toml",
        )

    def _request(
        self,
        *,
        policy: ActionPolicy,
        skill_snapshots=(),
        session_key: str = "",
    ):
        return replace(
            company_request(
                (task("only"),),
                final_task_id="only",
                roster=_ROSTER,
            ),
            goal="review repository acceptance",
            action_policy=policy,
            job_local_skill_snapshots=tuple(skill_snapshots),
            session_key=session_key,
        )

    def test_reassembles_exact_local_skill_and_session_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skills" / "review"
            (skill / "references").mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: review\n---\nUse the acceptance reference.",
                encoding="utf-8",
            )
            (skill / "references" / "acceptance.md").write_text(
                "Run the exact checks.\n",
                encoding="utf-8",
            )
            config = self._config(root, skill_dirs=(root / "skills",))
            selected = select_external_skills(
                discover_external_skills(config.external_skill_dirs),
                query=config.goal,
                limit=3,
            )
            policy = ActionPolicy(
                tool_grants=(
                    ToolGrant(
                        ExternalSkillPackageTools.tool_name,
                        (ToolEffect.READ,),
                    ),
                    ToolGrant(
                        "search_company_session_memory",
                        (ToolEffect.READ,),
                    ),
                    ToolGrant(
                        "read_company_session_memory",
                        (ToolEffect.READ,),
                    ),
                )
            )
            request = self._request(
                policy=policy,
                skill_snapshots=tuple(item.snapshot for item in selected),
                session_key="session-current",
            )

            assembly = assemble_continuation_capabilities(
                config=config,
                request=request,
                run_store=object(),
                company_store=object(),
                workspace_id="noruct-workspace",
                graph_decision=False,
            )
            try:
                digest, count = granted_tool_contract_digest(
                    assembly.registry,
                    request.action_policy,
                )
                self.assertRegex(digest, r"^[0-9a-f]{64}$")
                self.assertEqual(count, 3)
                self.assertEqual(assembly.selected_external_skill_count, 1)
                self.assertIsNotNone(assembly.session_store)
            finally:
                assert assembly.session_store is not None
                assembly.session_store.close()

    def test_support_file_drift_rejects_the_frozen_capability_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skills" / "review"
            (skill / "references").mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: review\n---\nUse the acceptance reference.",
                encoding="utf-8",
            )
            reference = skill / "references" / "acceptance.md"
            reference.write_text("Original checks.\n", encoding="utf-8")
            config = self._config(root, skill_dirs=(root / "skills",))
            selected = select_external_skills(
                discover_external_skills(config.external_skill_dirs),
                query=config.goal,
                limit=3,
            )
            policy = ActionPolicy(
                tool_grants=(
                    ToolGrant(
                        ExternalSkillPackageTools.tool_name,
                        (ToolEffect.READ,),
                    ),
                )
            )
            request = self._request(
                policy=policy,
                skill_snapshots=tuple(item.snapshot for item in selected),
            )
            reference.write_text("Drifted checks.\n", encoding="utf-8")

            with self.assertRaises(ContinuationRuntimePreflightError) as raised:
                assemble_continuation_capabilities(
                    config=config,
                    request=request,
                    run_store=object(),
                    company_store=object(),
                    workspace_id="noruct-workspace",
                    graph_decision=False,
                )

            self.assertEqual(
                raised.exception.code,
                ContinuationRuntimePreflightCode.CAPABILITY_MANIFEST_MISMATCH,
            )

    def test_removed_external_skill_root_refuses_resume_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skills" / "review"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: review\n---\nUse the exact release checklist.", encoding="utf-8"
            )
            config = self._config(root, skill_dirs=(root / "skills",))
            selected = select_external_skills(
                discover_external_skills(config.external_skill_dirs), query=config.goal, limit=3
            )
            request = self._request(
                policy=ActionPolicy(
                    tool_grants=(ToolGrant(ExternalSkillPackageTools.tool_name, (ToolEffect.READ,)),)
                ),
                skill_snapshots=tuple(item.snapshot for item in selected),
            )
            blueprint_path = root / "blueprints.sqlite3"
            registry = SQLiteGraphBlueprintRegistry(blueprint_path)
            try:
                blueprint = registry.save(
                    GraphBlueprint(
                        blueprint_id="skill-removal-safe",
                        version=1,
                        objective_class="general",
                        execution_profiles=("read_only",),
                        parameters=("objective",),
                        tasks=(
                            GraphBlueprintTask(
                                task_id="final",
                                objective_template="Review {{objective}}",
                                depends_on=(),
                                required_capabilities=("analysis",),
                                acceptance_templates=("A bounded review",),
                            ),
                        ),
                        final_task_id="final",
                        origin=GraphBlueprintOrigin.DRAFT,
                    )
                )
                registry.pin("operator", blueprint.ref)
            finally:
                registry.close()
            shutil.rmtree(root / "skills")

            with self.assertRaises(ContinuationRuntimePreflightError) as raised:
                assemble_continuation_capabilities(
                    config=config,
                    request=request,
                    run_store=object(),
                    company_store=object(),
                    workspace_id="noruct-workspace",
                    graph_decision=False,
                )

            self.assertEqual(
                raised.exception.code,
                ContinuationRuntimePreflightCode.CAPABILITY_MANIFEST_MISMATCH,
            )
            reopened = SQLiteGraphBlueprintRegistry(blueprint_path)
            try:
                pinned = reopened.pinned("operator")
                self.assertIsNotNone(pinned)
                assert pinned is not None
                self.assertEqual(reopened.get(pinned).ref, blueprint.ref)
            finally:
                reopened.close()

    def test_dynamic_mcp_grant_stays_dangling_without_discovery(self) -> None:
        class DiscoveryMustNotRun:
            def selected_runtime_tool_names(self):
                raise AssertionError("continuation must not discover MCP tools")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, mcp_read_only=DiscoveryMustNotRun())
            request = self._request(
                policy=ActionPolicy(
                    tool_grants=(
                        ToolGrant("mcp_dynamic_read", (ToolEffect.NETWORK,)),
                    )
                )
            )

            assembly = assemble_continuation_capabilities(
                config=config,
                request=request,
                run_store=object(),
                company_store=object(),
                workspace_id="noruct-workspace",
                graph_decision=True,
            )
            self.assertIsNone(assembly.registry.get("mcp_dynamic_read"))
            with self.assertRaises(ContinuationRuntimePreflightError) as raised:
                granted_tool_contract_digest(
                    assembly.registry,
                    request.action_policy,
                )
            self.assertEqual(
                raised.exception.code,
                ContinuationRuntimePreflightCode.TOOL_CONTRACT_INVALID,
            )

    def test_removed_mcp_profile_preserves_saved_blueprint_and_refuses_old_job_dispatch(self) -> None:
        """Removing a user setting is not a destructive Blueprint operation.

        The old Job's dynamic-MCP grant is intentionally unreconstructable
        without discovery, so it stops at the static contract check.  The
        independent immutable Blueprint registry remains readable for a later
        explicitly rebound Work Order.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = McpReadOnlyConfig(
                python_command=Path(sys.executable).resolve(),
                server_command=Path(sys.executable).resolve(),
                server_args=("-m", "fixture"),
                tool_name="read_issue",
            )
            config_path = root / "config.toml"
            write_mcp_settings(config_path, configured)
            blueprint_path = root / "blueprints.sqlite3"
            registry = SQLiteGraphBlueprintRegistry(blueprint_path)
            try:
                blueprint = registry.save(
                    GraphBlueprint(
                        blueprint_id="mcp-removal-safe",
                        version=1,
                        objective_class="general",
                        execution_profiles=("read_only",),
                        parameters=("objective",),
                        tasks=(
                            GraphBlueprintTask(
                                task_id="final",
                                objective_template="Review {{objective}}",
                                depends_on=(),
                                required_capabilities=("analysis",),
                                acceptance_templates=("A bounded review",),
                            ),
                        ),
                        final_task_id="final",
                        origin=GraphBlueprintOrigin.DRAFT,
                    )
                )
                registry.pin("operator", blueprint.ref)
            finally:
                registry.close()
            request = self._request(
                policy=ActionPolicy(
                    tool_grants=(ToolGrant("mcp_dynamic_read", (ToolEffect.NETWORK,)),)
                )
            )
            self.assertTrue(remove_mcp_settings(config_path))

            assembly = assemble_continuation_capabilities(
                config=self._config(root, mcp_read_only=None),
                request=request,
                run_store=object(),
                company_store=object(),
                workspace_id="noruct-workspace",
                graph_decision=True,
            )
            with self.assertRaises(ContinuationRuntimePreflightError) as raised:
                granted_tool_contract_digest(assembly.registry, request.action_policy)
            self.assertEqual(raised.exception.code, ContinuationRuntimePreflightCode.TOOL_CONTRACT_INVALID)
            reopened = SQLiteGraphBlueprintRegistry(blueprint_path)
            try:
                pinned = reopened.pinned("operator")
                self.assertIsNotNone(pinned)
                assert pinned is not None
                self.assertEqual(reopened.get(pinned).ref, blueprint.ref)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
