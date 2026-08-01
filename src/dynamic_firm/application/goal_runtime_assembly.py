"""Tool/session registry assembly for one already-authorized goal execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dynamic_firm.home_assistant import HomeAssistantTools
from dynamic_firm.product.external_skills import ExternalSkillPackageTools, discover_external_skills, select_external_skills
from dynamic_firm.product.sessions import CompanySessionStore
from dynamic_firm.product.settings_registry import SettingsRegistry
from dynamic_firm.runtime.company_session_recall import CompanySessionRecallTools
from dynamic_firm.runtime.container_workspace import ContainerWorkspaceTools
from dynamic_firm.runtime.knowledge_tools import KnowledgeRuntimeTools
from dynamic_firm.runtime.manager_tools import ManagerRuntimeTools
from dynamic_firm.runtime.remote_workspace import RemoteWorkspaceTools
from dynamic_firm.runtime.tools import ToolRegistry, WorkspaceReadTools, WorkspaceTools


def assemble_goal_tool_registry(
    *,
    state_path: Path,
    workspace: Path,
    config_path: Path,
    goal: str,
    external_skill_dirs: tuple[Path, ...],
    permission_mode: str,
    capability_lane: bool,
    session_key: str,
    manager_assignment: object | None,
    company_store: Any,
    run_store: Any,
    job_id: str,
    remote_worker: Any,
    container_workspace: Any,
    executable_plugins: Any,
    home_assistant: Any,
    workspace_id: str,
) -> tuple[ToolRegistry, CompanySessionStore | None]:
    """Compose tools only; Store, approval, Kernel and provider stay owned by caller."""

    registry = ToolRegistry()
    selected = select_external_skills(
        discover_external_skills(external_skill_dirs), query=goal, limit=3
    )
    for definition in ExternalSkillPackageTools(selected).definitions():
        registry.register(definition)
    if capability_lane:
        for definition in SettingsRegistry(config_path).tool_definitions():
            registry.register(definition)
        knowledge_tools = KnowledgeRuntimeTools(
            state_path=state_path,
            workspace=workspace,
        )
        for definition in knowledge_tools.definitions():
            registry.register(definition)
    workspace_tools = (
        WorkspaceTools({workspace_id: workspace})
        if permission_mode == "ask"
        else WorkspaceReadTools({workspace_id: workspace})
    )
    for definition in workspace_tools.definitions():
        registry.register(definition)
    session_store = None
    if session_key:
        session_store = CompanySessionStore(state_path)
        session_tools = CompanySessionRecallTools(
            session_store,
            current_session_id=session_key,
        )
        for definition in session_tools.definitions():
            registry.register(definition)
    if manager_assignment is not None:
        manager_tools = ManagerRuntimeTools(
            company_store=company_store,
            run_store=run_store,
            runtime_state_path=state_path,
            current_job_id=job_id,
        )
        for definition in manager_tools.definitions():
            registry.register(definition)
    if remote_worker is not None:
        registry.register(RemoteWorkspaceTools(remote_worker).definition())
    if container_workspace is not None:
        registry.register(ContainerWorkspaceTools(container_workspace, workspace).definition())
    if executable_plugins is not None:
        for plugin in executable_plugins.plugins:
            for definition in plugin.definitions():
                registry.register(definition)
    if home_assistant is not None:
        for definition in HomeAssistantTools(home_assistant).definitions():
            registry.register(definition)
    return registry, session_store
