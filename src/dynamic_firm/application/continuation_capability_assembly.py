"""Provider-free static capability reassembly for same-Job continuation.

The retained ActionPolicy is the authority for what the old Job may use.  This
module reconstructs only definitions whose contracts can be derived from
local frozen request/config state without contacting MCP servers, browser
bridges, model providers, or any other external system.  The ordinary runtime
binding preflight still performs the final exact digest comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dynamic_firm.application.continuation_runtime_preflight import (
    ContinuationRuntimePreflightCode,
    ContinuationRuntimePreflightError,
)
from dynamic_firm.browser_connector import BrowserReadOnlyConnector
from dynamic_firm.computer_use_connector import ComputerUseConnector
from dynamic_firm.home_assistant import HomeAssistantTools
from dynamic_firm.kernel.models import CompanyRunRequest
from dynamic_firm.openai_media import OpenAIMediaConnector
from dynamic_firm.product.external_skills import (
    ExternalSkillInfo,
    ExternalSkillPackageTools,
    discover_external_skills,
    select_external_skills,
)
from dynamic_firm.product.sessions import CompanySessionStore
from dynamic_firm.product.settings_registry import SettingsRegistry
from dynamic_firm.runtime.company_session_recall import CompanySessionRecallTools
from dynamic_firm.runtime.container_workspace import ContainerWorkspaceTools
from dynamic_firm.runtime.knowledge_tools import KnowledgeRuntimeTools
from dynamic_firm.runtime.manager_tools import ManagerRuntimeTools
from dynamic_firm.runtime.remote_workspace import RemoteWorkspaceTools
from dynamic_firm.runtime.tool_contracts import ToolRegistry
from dynamic_firm.runtime.workspace_mutation_tools import WorkspaceTools
from dynamic_firm.runtime.workspace_read_tools import WorkspaceReadTools
from dynamic_firm.web_read import WebReadConnector
from dynamic_firm.web_search import SearxngSearchConnector


@dataclass(frozen=True, slots=True)
class ContinuationCapabilityAssembly:
    """Static registry plus caller-owned local resources that need closing."""

    registry: ToolRegistry
    session_store: CompanySessionStore | None
    selected_external_skill_count: int


def assemble_continuation_capabilities(
    *,
    config: Any,
    request: CompanyRunRequest,
    run_store: Any,
    company_store: Any,
    workspace_id: str,
    graph_decision: bool,
) -> ContinuationCapabilityAssembly:
    """Recreate the external-call-free portion of one frozen tool surface.

    Graph decisions may need configured static connector definitions after
    approval.  Constructing those definitions validates local configuration
    but performs no transport call.  MCP read/action connectors are
    deliberately absent because their definitions require live discovery;
    any corresponding frozen grant therefore remains dangling and the exact
    runtime preflight refuses the continuation.
    """

    registry = ToolRegistry()
    session_store: CompanySessionStore | None = None
    try:
        _register(registry, SettingsRegistry(config.config_path).tool_definitions())
        _register(
            registry,
            KnowledgeRuntimeTools(
                state_path=config.state_path,
                workspace=config.workspace,
            ).definitions(),
        )
        workspace_tools = (
            WorkspaceTools({workspace_id: config.workspace})
            if graph_decision
            else WorkspaceReadTools({workspace_id: config.workspace})
        )
        _register(registry, workspace_tools.definitions())

        selected_skills = _frozen_external_skill_packages(config, request)
        if _has_grant(request, ExternalSkillPackageTools.tool_name):
            _register(
                registry,
                ExternalSkillPackageTools(selected_skills).definitions(),
            )

        if request.session_key:
            session_store = CompanySessionStore(config.state_path)
            _register(
                registry,
                CompanySessionRecallTools(
                    session_store,
                    current_session_id=request.session_key,
                ).definitions(),
            )
        if request.manager_employee_id:
            _register(
                registry,
                ManagerRuntimeTools(
                    company_store=company_store,
                    run_store=run_store,
                    runtime_state_path=config.state_path,
                    current_job_id=request.job_id,
                ).definitions(),
            )

        if graph_decision:
            _register_graph_static_connectors(
                registry,
                config=config,
                workspace_id=workspace_id,
            )
        return ContinuationCapabilityAssembly(
            registry=registry,
            session_store=session_store,
            selected_external_skill_count=len(selected_skills),
        )
    except Exception:
        if session_store is not None:
            session_store.close()
        raise


def _frozen_external_skill_packages(
    config: Any,
    request: CompanyRunRequest,
) -> tuple[ExternalSkillInfo, ...]:
    """Resolve only a granted package tool and prove its exact local closure."""

    if not _has_grant(request, ExternalSkillPackageTools.tool_name):
        return ()
    selected = select_external_skills(
        discover_external_skills(config.external_skill_dirs),
        query=request.goal,
        limit=3,
    )
    current = tuple(item.snapshot for item in selected)
    expected = tuple(
        item
        for item in request.job_local_skill_snapshots
        if item.content_id.startswith("external-skill:")
    )
    if current != expected:
        raise ContinuationRuntimePreflightError(
            ContinuationRuntimePreflightCode.CAPABILITY_MANIFEST_MISMATCH
        )
    return selected


def _register_graph_static_connectors(
    registry: ToolRegistry,
    *,
    config: Any,
    workspace_id: str,
) -> None:
    """Register current local definitions without discovery or transport I/O."""

    if config.browser_read_only is not None:
        _register(registry, BrowserReadOnlyConnector(config.browser_read_only).definitions())
    if config.computer_use is not None:
        _register(registry, ComputerUseConnector(config.computer_use).definitions())
    if config.openai_media is not None:
        _register(
            registry,
            OpenAIMediaConnector(
                config.openai_media,
                config.workspace,
                workspace_id=workspace_id,
            ).definitions(),
        )
    if config.web_read is not None:
        registry.register(WebReadConnector(config.web_read).definition())
    if config.web_search is not None:
        registry.register(SearxngSearchConnector(config.web_search).definition())
    if config.remote_worker is not None:
        registry.register(RemoteWorkspaceTools(config.remote_worker).definition())
    if config.container_workspace is not None:
        registry.register(
            ContainerWorkspaceTools(
                config.container_workspace,
                config.workspace,
            ).definition()
        )
    if config.executable_plugins is not None:
        for plugin in config.executable_plugins.plugins:
            _register(registry, plugin.definitions())
    if config.home_assistant is not None:
        _register(registry, HomeAssistantTools(config.home_assistant).definitions())


def _has_grant(request: CompanyRunRequest, tool_name: str) -> bool:
    return any(
        grant.tool_name == tool_name for grant in request.action_policy.tool_grants
    )


def _register(registry: ToolRegistry, definitions: Any) -> None:
    for definition in definitions:
        registry.register(definition)
