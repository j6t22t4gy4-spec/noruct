from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import io
import importlib.metadata
import json
import os
import platform
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
import uuid
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence, TextIO

from dynamic_firm import __version__
from dynamic_firm.application import GoalExecutionServices
from dynamic_firm.application.connectivity_cli import (
    browser_status_record as _browser_status_record,
    computer_use_status_record as _computer_use_status_record,
    media_status_record as _media_status_record,
    run_browser_command as _run_browser_command,
    run_computer_use_command as _run_computer_use_command,
    run_home_assistant_command as _run_home_assistant_command,
    run_media_command as _run_media_command,
    run_web_search_command as _run_web_search_command,
    web_search_status_record as _web_search_status_record,
)
from dynamic_firm.application.mcp_cli import (
    McpCliPorts,
    mcp_action_status_record as _mcp_action_status_record,
    mcp_status_record as _mcp_status_record,
    run_mcp_command as _run_mcp_command,
)
from dynamic_firm.application.plugin_cli import (
    plugin_root as _plugin_root,
    plugin_status_record as _plugin_status_record,
    run_plugin_command as _run_plugin_command,
)
from dynamic_firm.application.company_cli import run_company_command
from dynamic_firm.application.evaluation_core_cli import (
    _run_active_job_ledger_evaluation,
    _run_alpha_readiness_evaluation,
    _run_causal_workflow_evaluation,
    _run_information_boundary_evaluation,
    _run_information_boundary_pair_evaluation,
    _run_information_boundary_v4_evaluation,
    _run_organization_admission_evaluation,
    _run_release_authorization_pair_evaluation,
)
from dynamic_firm.application.evaluation_workflow_cli import (
    _run_workflow_patch_cohort_evaluation,
    _run_workflow_patch_efficiency_evaluation,
    _run_workflow_patch_extension_evaluation,
)
from dynamic_firm.application.evaluation_firm_cli import (
    _run_exact_context_live_pair_evaluation,
    _run_firm_value_campaign_evaluation,
    _run_firm_value_campaign_v2_evaluation,
    _run_firm_value_evaluation,
    _run_firm_value_v2_evaluation,
)
from dynamic_firm.application.modern_terminal_graph import (
    graph_control_snapshot,
    save_future_graph_constraints,
)
from dynamic_firm.application.modern_terminal_job_audit import (
    job_audit_catalog,
    job_audit_snapshot,
)
from dynamic_firm.application.modern_terminal_controller import (
    ModernControllerPorts,
    ModernInteractiveController,
)
from dynamic_firm.coding import (
    APPLY_CHANGE_SET_TOOL,
    ChangeSetCatalog,
    CodingWorkerPort,
    RoutedEmployeeExecutionService,
    ShadowCodingEmployeeRuntimeService,
    ShadowWorkspaceService,
)
from dynamic_firm.company import (
    ActiveRosterSnapshot,
    company_final_report,
    manager_operating_report,
    ManagerOutcomeDecision,
    AuthoritySnapshotIdentity,
    CompanyLearningService,
    DirectCompanyExecutor,
    GraphBlueprintControlService,
    CommunityBlueprintRegistry,
    GraphMutationPolicy,
    GraphUserConstraints,
    CompanyStateStore,
    CompanyVersion,
    EmployeeSkillProcedure,
    EvidenceSource,
    FirmAdmissionController,
    HireObservationService,
    EmployeeSkillPatchService,
    EvolutionAutonomyMode,
    HiringRecommendationService,
    MANAGER_CAPABILITY,
    ManagerDelegation,
    PersistentExecutiveManager,
    RetentionReviewMode,
    RosterSnapshotError,
    RosterPatchOperation,
    RosterPatchService,
    SQLiteGraphBlueprintRegistry,
    blueprint_passport_from_payload,
    build_qualified_blueprint_passport,
    community_release_from_payload,
    materialize_staged_blueprint,
    WorkOrderBudgetSnapshot,
    WORKSPACE_STRUCTURE_PROJECTION_REVISION,
    WorkspaceProjectionError,
    decode_active_roster,
    episode_from_runtime_ledger,
    assess_manager_outcomes,
    assess_organization_outcomes,
    apply_organization_evidence_gate,
    organization_outcome_metrics,
    project_workspace_structure,
    staffing_demands_from_runtime_ledger,
    workflow_context_fingerprint_v2,
    normalize_work_order,
    graph_run_record_from_active_job,
    verify_work_order_binding,
)
from dynamic_firm.evolution.community_graph_codec import (
    community_release_from_evolution_artifact,
    community_release_to_evolution_artifact,
)
from dynamic_firm.company.work_order_portfolio import WorkOrderPortfolioStore
from dynamic_firm.company.operating import (
    CompanyOperatingDecision,
    CompanyWorkMode,
    InitialCoordinationPolicy,
    RequestedEffect,
    classify_company_input,
)
from dynamic_firm.company.coordinator import ManagerProposalAdapter, FirmCoordinatorAction
from dynamic_firm.compiler import (
    CompilerDecision,
    CompilerExecutionProfile,
    CompilerReason,
    CompilerRequest,
    PlanningOwner,
    PlanningMode,
    direct_conversation_decision,
    solo_first_decision,
)
from dynamic_firm.evolution import (
    EvolutionNetworkService,
    EvolutionStore,
    UnsupportedEvolutionStoreSchemaError,
)
from dynamic_firm.network import (
    NETWORK_PUBLISHER_CLASSES,
    NETWORK_UPDATE_MODES,
    NoructNetworkService,
)
from dynamic_firm.evolution.runtime_adapter import (
    EvolutionRuntimeArtifactAdapter,
    RuntimeArtifactResolution,
    merge_employee_skill_snapshots,
    project_network_workflow_priors,
    runtime_artifact_scopes,
)
from dynamic_firm.evolution.mcp_package import build_mcp_policy_artifact, mcp_policy_binding_digest_from_artifact, mcp_policy_profile
from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    ExecutionOriginBinding,
    ExecutionReplicaPreference,
    JobLimits,
    JobResult,
    JobStatus,
)
from dynamic_firm.kernel.mutation import content_digest as kernel_content_digest
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.knowledge import (
    AttributionStatus,
    AssetStatus,
    ContentTrustClass,
    DecisionStatus,
    EpistemicStatus,
    IntentStatus,
    KnowledgeExecutionOutcome,
    KnowledgeFirmBridge,
    KnowledgeStore,
    OutcomeVerdict,
    QuestionStatus,
    ResearchRequestStatus,
    knowledge_runtime_paths,
)
from dynamic_firm.knowledge.lifecycle import (
    authorize_knowledge_deletion,
    delete_knowledge_state,
    export_knowledge_archive,
    knowledge_diagnostics,
    restore_knowledge_archive,
)
from dynamic_firm.knowledge.service import UserKnowledgeService
from dynamic_firm.knowledge.vault import KnowledgeVault
from dynamic_firm.runtime.company_budget import (
    CompanyCostBudgetPolicy,
    SQLiteCompanyBudgetAuthority,
)
from dynamic_firm.mcp_connector import (
    AUDITED_MCP_VERSION,
    EXTERNAL_READ_TOOL,
    McpReadOnlyConfig,
    McpActionConfig,
    McpActionConfigSet,
    McpActionConnector,
    McpActionConnectorGroup,
    McpActionPolicy,
    McpReadOnlyConnector,
    McpReadOnlyConnectorGroup,
    McpReadOnlyConfigSet,
    McpReadOnlyPolicy,
    configured_sdk_version,
    configured_sdk_versions,
    config_from_settings as mcp_config_from_settings,
    mcp_action_config_from_settings,
    mcp_action_config_for_profile,
    mcp_action_configs,
    mcp_action_runtime_tool_names,
    session_binding_digest as mcp_session_binding_digest,
)
from dynamic_firm.browser_connector import (
    BrowserReadOnlyConfig,
    BrowserReadOnlyConnector,
    browser_config_from_settings,
    configured_node_version,
)
from dynamic_firm.computer_use_connector import (
    ComputerUseConfig,
    ComputerUseConnector,
    computer_use_config_from_settings,
    configured_driver_version,
)
from dynamic_firm.openai_media import (
    OpenAIMediaConfig,
    OpenAIMediaConnector,
    media_config_from_settings,
)
from dynamic_firm.product.openai_media_settings import (
    remove_media_settings,
    write_media_settings,
)
from dynamic_firm.product.terminal import strip_ansi as strip_terminal_escapes
from dynamic_firm.web_search import (
    WEB_SEARCH_TOOL,
    SearxngSearchConfig,
    SearxngSearchConnector,
    config_from_settings as web_search_config_from_settings,
)
from dynamic_firm.home_assistant import (
    HomeAssistantConfig,
    HomeAssistantTools,
    config_from_settings as home_assistant_config_from_settings,
    remove_home_assistant_settings,
    status as home_assistant_status,
    write_home_assistant_settings,
)
from dynamic_firm.web_read import (
    WEB_READ_TOOL,
    WebReadConnector,
    WebReadConfig,
    config_from_settings as web_read_config_from_settings,
)
from dynamic_firm._vendor.runtime_safety.redact import redact_terminal_output
from dynamic_firm.providers.codex_exec import (
    CodexExecCodingWorker,
    CodexExecProvider,
    CodexExecProviderConfig,
)
from dynamic_firm.providers.external_exec import ExternalExecProvider, ExternalExecProviderConfig
from dynamic_firm.providers.anthropic import (
    AnthropicProvider,
    AnthropicProviderConfig,
)
from dynamic_firm.providers.openai_compat import (
    OpenAICompatProvider,
    OpenAICompatProviderConfig,
)
from dynamic_firm.providers.vertex import VertexProvider, VertexProviderConfig
from dynamic_firm.providers.bedrock import BedrockProvider, BedrockProviderConfig
from dynamic_firm.providers.fallback import FallbackModelProvider, FallbackProviderConfig
from dynamic_firm.providers.moa import MixtureOfAgentsProvider, MoAProviderConfig
from dynamic_firm.providers.profiles import (
    PROVIDER_KINDS,
    PROVIDER_SETUP_OPTIONS,
    provider_profile,
)
from dynamic_firm.providers.preflight import (
    ProviderPreflightConfig,
    probe_provider_metadata,
    provider_preflight_status,
)
from dynamic_firm.product import (
    CompanySession,
    CompanySessionStore,
    InputRoute,
    InlineTerminalUI,
    InteractiveApprovalController,
    LiveTerminalUI,
    ProductEvent,
    ProductEventType,
    SetupConfig,
    GatewayServiceStore,
    gateway_service_state_path,
    ScheduleServiceStore,
    schedule_service_state_path,
    serve_gateway_dashboard,
    serve_graph_workbench_dashboard,
    browse_company_sessions,
    product_event_from_mutation,
    product_event_from_assignment,
    product_event_from_graph_patch,
    product_event_from_graph_patch_proposal,
    product_event_from_run,
    route_interactive_input,
    write_setup_config,
    write_mcp_settings,
    append_mcp_settings,
    remove_mcp_profile_settings,
    remove_mcp_settings,
    append_mcp_action_settings,
    configured_mcp_action_policy,
    remove_mcp_action_profile_settings,
    remove_mcp_action_settings,
    write_mcp_action_settings,
    SlackChannelConfig,
    deliver_slack_message,
    remove_slack_channel_settings,
    slack_channel_config_from_settings,
    slack_channel_status,
    write_slack_channel_settings,
    SlackInboundConfig,
    SlackInboundMessage,
    SlackInboundStore,
    run_slack_inbound_channel,
    slack_inbound_config_from_settings,
    slack_inbound_status,
    slack_inbound_state_path,
    remove_slack_inbound_settings,
    write_slack_inbound_settings,
    DiscordChannelConfig,
    deliver_discord_message,
    remove_discord_channel_settings,
    discord_channel_config_from_settings,
    discord_channel_status,
    write_discord_channel_settings,
    DiscordInboundConfig,
    DiscordInboundMessage,
    DiscordInboundStore,
    discord_inbound_config_from_settings,
    discord_inbound_state_path,
    discord_inbound_status,
    remove_discord_inbound_settings,
    run_discord_inbound_channel,
    write_discord_inbound_settings,
    NtfyChannelConfig,
    deliver_ntfy_message,
    ntfy_channel_config_from_settings,
    ntfy_channel_status,
    remove_ntfy_channel_settings,
    write_ntfy_channel_settings,
    NtfyInboundConfig,
    NtfyInboundMessage,
    ntfy_inbound_config_from_settings,
    ntfy_inbound_status,
    remove_ntfy_inbound_settings,
    run_ntfy_inbound,
    write_ntfy_inbound_settings,
    EmailChannelConfig,
    deliver_email_message,
    email_channel_config_from_settings,
    email_channel_status,
    remove_email_channel_settings,
    write_email_channel_settings,
    EmailInboundConfig,
    EmailInboundMessage,
    email_inbound_config_from_settings,
    email_inbound_status,
    remove_email_inbound_settings,
    run_email_inbound,
    write_email_inbound_settings,
    MattermostChannelConfig,
    deliver_mattermost_message,
    mattermost_channel_config_from_settings,
    mattermost_channel_status,
    remove_mattermost_channel_settings,
    write_mattermost_channel_settings,
    MattermostInboundConfig,
    MattermostInboundCursorStore,
    MattermostInboundMessage,
    mattermost_inbound_config_from_settings,
    mattermost_inbound_state_path,
    mattermost_inbound_status,
    remove_mattermost_inbound_settings,
    run_mattermost_inbound,
    write_mattermost_inbound_settings,
    MatrixChannelConfig,
    deliver_matrix_message,
    matrix_channel_config_from_settings,
    matrix_channel_status,
    remove_matrix_channel_settings,
    write_matrix_channel_settings,
    MatrixInboundConfig,
    MatrixInboundCursorStore,
    MatrixInboundMessage,
    matrix_inbound_config_from_settings,
    matrix_inbound_state_path,
    matrix_inbound_status,
    remove_matrix_inbound_settings,
    run_matrix_inbound,
    write_matrix_inbound_settings,
    DingTalkChannelConfig, deliver_dingtalk_message, dingtalk_channel_config_from_settings,
    dingtalk_channel_status, remove_dingtalk_channel_settings, write_dingtalk_channel_settings,
    TeamsChannelConfig, deliver_teams_message, remove_teams_channel_settings,
    teams_channel_config_from_settings, teams_channel_status, write_teams_channel_settings,
    execution_environment_status,
    probe_ssh_environment,
    run_ssh_operator_command,
    inspect_workspace_snapshot_manifest,
    transfer_workspace_snapshot,
    write_workspace_snapshot_manifest,
    activate_installed_release,
    release_installation_status,
    ChannelConfig,
    ChannelJobSummary,
    channel_config_from_settings,
    channel_status,
    deliver_channel_test,
    deliver_terminal_job_summary,
    remove_channel_settings,
    write_channel_settings,
    InboundChannelConfig,
    InboundMessage,
    InboundMessageStore,
    consume_inbound_channel,
    inbound_channel_config_from_settings,
    inbound_channel_status,
    inbound_state_path,
    remove_inbound_channel_settings,
    write_inbound_channel_settings,
    AcpApprovalPort,
    serve_acp_stdio,
    TelegramChannelConfig,
    TelegramChannelStore,
    TelegramInboundMessage,
    run_telegram_channel,
    telegram_channel_config_from_settings,
    telegram_channel_status,
    telegram_state_path,
    remove_telegram_channel_settings,
    write_telegram_channel_settings,
    configured_browser_policy,
    remove_browser_settings,
    write_browser_settings,
    browser_lifecycle_status,
    close_isolated_browser,
    launch_isolated_browser,
    lifecycle_state_path,
    configured_computer_use_policy,
    remove_computer_use_settings,
    write_computer_use_settings,
    RemoteWorkerSettings,
    remove_remote_worker_settings,
    remote_worker_status,
    write_remote_worker_settings,
    ContainerSettings,
    container_status,
    remove_container_settings,
    write_container_settings,
    configured_web_search_policy,
    remove_web_search_settings,
    write_web_search_settings,
    ExecutablePluginStore,
    PluginLifecycleError,
    PluginRuntimeConfig,
    configured_plugin_runtime,
    plugin_config_from_settings,
    remove_plugin_settings,
    write_plugin_settings,
)
from dynamic_firm.product.global_settings import (
    GlobalRuntimeSettings,
    write_global_runtime_settings,
)
from dynamic_firm.product.settings_registry import SettingsEntry, SettingsRegistry
from dynamic_firm.product.company_coordination_settings import (
    CompanyCoordinationSettings,
    company_coordination_enrollment_preview,
    company_coordination_preflight,
    company_coordination_config_from_settings,
)
from dynamic_firm.runtime.company_coordination import RemoteCompanyCoordinationClient
from dynamic_firm.product.settings_dashboard import page_for_entry
from dynamic_firm.product.operator_surface import build_operator_surface_snapshot
from dynamic_firm.product.modern_tui import (
    ModernTerminalCommandResult,
    ModernTerminalResult,
    ModernTerminalSnapshot,
    ModernTerminalUnavailable,
    modern_terminal_available,
    modern_terminal_install_hint,
    run_modern_terminal,
)
from dynamic_firm.product.terminal_diagnostics import modern_terminal_crash_log_path
from dynamic_firm.product.data_commands import run_data_command
from dynamic_firm.product.company_commands import (
    parse_operator_timestamp,
    propose_roster_patch,
    render_company_observability,
    render_roster_patch_preview,
    run_company_curate_daemon,
)
from dynamic_firm.product.knowledge_cli_values import knowledge_limit, show_knowledge_value
from dynamic_firm.product.graph_cli_values import (
    community_blueprint_registry_path,
    graph_constraints_from_args,
    graph_registry_path,
)
from dynamic_firm.product.session_bindings import (
    session_cost_mode_binding,
    session_mcp_binding,
    session_provider_binding,
)
from dynamic_firm.product.manager_planning import build_manager_planning_brief
from dynamic_firm.product.models import ModelOption, filter_model_options, model_options
from dynamic_firm.runtime.models import (
    ActionPolicy,
    ContextBundle,
    CostEfficiencyMode,
    RunLimits,
    TaskEvidencePack,
    ToolEffect,
    ToolGrant,
    Usage,
    VersionedContent,
    to_primitive,
)
from dynamic_firm.runtime.job_ledger import (
    ActiveJobInspector,
    SQLiteActiveJobLedger,
)
from dynamic_firm.runtime.operator_attention import CompanyAttentionInspector
from dynamic_firm.runtime.ports import ApprovalPort, CancellationToken, ModelProviderPort
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.company_session_recall import CompanySessionRecallTools
from dynamic_firm.runtime.knowledge_tools import KnowledgeRuntimeTools
from dynamic_firm.runtime.manager_tools import ManagerRuntimeTools
from dynamic_firm.runtime.tools import ToolRegistry, WorkspaceReadTools, WorkspaceTools
from dynamic_firm.runtime.remote_workspace import RemoteWorkspaceTools, remote_worker_config_from_settings, RemoteWorkspaceWorkerConfig, verify_remote_workspace_worker, verify_remote_workspace_worker_content
from dynamic_firm.runtime.container_workspace import ContainerWorkspaceTools, container_config_from_settings, ContainerWorkspaceConfig, verify_container_workspace
from dynamic_firm.product.external_skills import (
    ExternalSkillPackageTools,
    discover_external_skills,
    external_skill_directories,
    load_external_skill_snapshots,
    select_external_skills,
)
from dynamic_firm.product.plugin_catalog import PluginCatalogSource, PluginCatalogStore
from dynamic_firm.product.schedules import ScheduleStore, ScheduledJob


EXIT_OK = 0
EXIT_INPUT = 2
EXIT_RUNTIME = 3
EXIT_JOB_FAILED = 4
WORKSPACE_ID = "noruct-workspace"
DEFAULT_CONFIG_PATH = Path.home() / ".noruct" / "config.toml"
DEFAULT_STATE_PATH = Path.home() / ".noruct" / "runtime.db"
DEFAULT_API_KEY_ENV = "NORUCT_API_KEY"


def _provider_cli_choices() -> tuple[str, ...]:
    return tuple(kind.replace("_", "-") for kind in PROVIDER_KINDS)
@dataclass(frozen=True, slots=True)
class RunCommandConfig:
    goal: str
    workspace: Path
    state_path: Path
    provider_kind: str
    base_url: str
    model: str
    codex_model: str | None
    codex_command: str
    api_key_env: str | None
    request_timeout_seconds: float
    permission_mode: str
    run_limits: RunLimits
    capability_trust_mode: str = "trusted"
    mcp_read_only: McpReadOnlyPolicy | None = None
    mcp_action: McpActionPolicy | None = None
    browser_read_only: BrowserReadOnlyConfig | None = None
    computer_use: ComputerUseConfig | None = None
    openai_media: OpenAIMediaConfig | None = None
    web_read: WebReadConfig | None = None
    web_search: SearxngSearchConfig | None = None
    home_assistant: HomeAssistantConfig | None = None
    executable_plugins: PluginRuntimeConfig | None = None
    employee_runtime: str = "noruct"
    runtime_python: str = ""
    remote_worker: RemoteWorkspaceWorkerConfig | None = None
    container_workspace: ContainerWorkspaceConfig | None = None
    external_skill_dirs: tuple[Path, ...] = ()
    external_command: str = ""
    fallback_routes: tuple[dict[str, object], ...] = ()
    moa_reference_routes: tuple[dict[str, object], ...] = ()
    config_path: Path = DEFAULT_CONFIG_PATH
    external_read_mode: str = "allow"
    external_state_mode: str = "ask"
    agent_settings_mode: str = "ask"
    company_coordination: RemoteCompanyCoordinationClient | None = None
    stale_timeout_seconds: float = 90.0


ProviderConfig = OpenAICompatProviderConfig | AnthropicProviderConfig | CodexExecProviderConfig | ExternalExecProviderConfig | VertexProviderConfig | BedrockProviderConfig | FallbackProviderConfig | MoAProviderConfig
ProviderFactory = Callable[[ProviderConfig], ModelProviderPort]
CodingWorkerFactory = Callable[[CodexExecProviderConfig], CodingWorkerPort]


def _add_execution_options(
    command: argparse.ArgumentParser,
    *,
    workspace_default: Path | None = Path.cwd(),
) -> None:
    command.add_argument(
        "--workspace",
        type=Path,
        default=workspace_default,
        help="Workspace visible to the company (default: current directory).",
    )
    command.add_argument(
        "--state",
        type=Path,
        default=None,
        help="SQLite runtime ledger path (default: ~/.noruct/runtime.db).",
    )
    command.add_argument(
        "--provider",
        dest="provider_kind",
        choices=_provider_cli_choices(),
        default=None,
        help="Model transport. Run `noruct setup` for the current named connection choices.",
    )
    command.add_argument(
        "--base-url",
        default=None,
        help="Provider API base URL override, or NORUCT_BASE_URL.",
    )
    command.add_argument(
        "--model",
        default=None,
        help="Model identifier, or NORUCT_MODEL.",
    )
    command.add_argument(
        "--codex-command",
        default=None,
        help="Codex executable name or absolute path, or NORUCT_CODEX_COMMAND.",
    )
    command.add_argument(
        "--external-command",
        default=None,
        help="User-managed noruct.external-model-exec.v1 executable, or NORUCT_EXTERNAL_COMMAND.",
    )
    command.add_argument(
        "--fallback",
        action="append",
        default=None,
        metavar="PROVIDER:MODEL",
        help="Explicit retryable-error fallback route; repeatable, up to four. Credentials remain in each provider's named environment variable.",
    )
    command.add_argument(
        "--moa-reference",
        action="append",
        default=None,
        metavar="PROVIDER:MODEL",
        help="Advisory model for Mixture of Agents; repeatable, one through eight. The active provider is the tool-calling aggregator.",
    )
    command.add_argument(
        "--api-key-env",
        default=None,
        help="Name of the environment variable containing the API key.",
    )
    command.add_argument(
        "--no-auth",
        action="store_true",
        default=None,
        help="Send no API credential; intended for loopback model servers.",
    )
    command.add_argument("--request-timeout", type=float, default=None, metavar="SECONDS")
    command.add_argument(
        "--stale-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="No-progress deadline for streamed Codex calls; progress events reset it.",
    )
    command.add_argument("--max-wall-time", type=float, default=None, metavar="SECONDS")
    command.add_argument("--max-model-calls", type=int, default=None)
    command.add_argument("--max-tool-calls", type=int, default=None)
    command.add_argument("--max-cost-usd", type=float, default=None)
    command.add_argument(
        "--cost-mode",
        choices=tuple(mode.value for mode in CostEfficiencyMode),
        default=None,
        help="Model-context policy: standard preserves tool output; economy compacts noisy successful reads only.",
    )
    command.add_argument(
        "--permission-mode",
        choices=("read-only", "ask"),
        default=None,
        help="Authority boundary; interactive terminals default to ask.",
    )
    command.add_argument(
        "--trust-mode",
        dest="capability_trust_mode",
        choices=("strict", "trusted", "autonomous"),
        default=None,
        help=(
            "Review friction for already granted tools: strict prompts for every "
            "effect, trusted auto-runs ordinary workspace and explicitly installed "
            "capabilities, autonomous auto-runs all enabled capabilities."
        ),
    )
    command.add_argument(
        "--employee-runtime",
        choices=("noruct",),
        default=None,
        help=(
            "Noruct Employee Runtime. This is the only supported employee execution path."
        ),
    )
    command.add_argument(
        "--runtime-python",
        default=None,
        help=(
            "Python executable containing the audited Noruct runtime dependencies, or "
            "NORUCT_RUNTIME_PYTHON. No dependency is installed automatically."
        ),
    )
    command.add_argument(
        "--skills-dir",
        action="append",
        default=None,
        metavar="PATH",
        help="Read compatible SKILL.md instructions from this user-owned directory for this Job. Repeatable.",
    )
    command.add_argument(
        "--plain",
        action="store_true",
        help="Disable ANSI styling, boxes, and live terminal rendering.",
    )
    command.add_argument(
        "--no-live-screen",
        action="store_true",
        help="Keep the styled scrollback-safe inline interface instead of the live viewport.",
    )
    command.add_argument(
        "--terminal-ui",
        choices=("auto", "native", "modern"),
        default=os.environ.get("NORUCT_TERMINAL_UI", "auto"),
        help=(
            "Interactive terminal surface. Auto uses the installed modern profile "
            "and otherwise keeps the dependency-free native surface."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noruct",
        description="Give one goal to Noruct.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("NORUCT_CONFIG", DEFAULT_CONFIG_PATH)),
        help="Non-secret TOML configuration path.",
    )
    commands = parser.add_subparsers(dest="command")

    from dynamic_firm.application.runtime_control_cli_parser import (
        add_runtime_control_commands,
    )

    add_runtime_control_commands(
        commands,
        add_execution_options=_add_execution_options,
    )
    def add_local_knowledge_options(item: argparse.ArgumentParser) -> None:
        item.add_argument("--state", type=Path, default=None)
        item.add_argument("--json", action="store_true")


    from dynamic_firm.application.knowledge_intent_cli_parser import (
        add_knowledge_intent_commands,
    )

    add_knowledge_intent_commands(
        commands,
        add_local_knowledge_options=add_local_knowledge_options,
        add_execution_options=_add_execution_options,
    )

    from dynamic_firm.application.evolution_cli_parser import (
        add_evolution_commands,
        add_evolution_paths,
    )

    add_evolution_commands(commands)
    from dynamic_firm.application.operator_control_cli_parser import (
        add_operator_control_commands,
    )

    add_operator_control_commands(
        commands,
        default_state_path=DEFAULT_STATE_PATH,
        provider_cli_choices=_provider_cli_choices,
        add_execution_options=_add_execution_options,
    )
    from dynamic_firm.application.integration_cli_parser import add_integration_commands

    add_integration_commands(commands)
    demo = commands.add_parser(
        "demo",
        help="Run an offline organization fixture without credentials or network access.",
    )
    demo.add_argument("fixture", choices=("solo", "parallel", "replan"))
    demo.add_argument(
        "--strategy",
        choices=("dynamic", "solo", "fixed"),
        default="dynamic",
    )
    demo.add_argument("--json", action="store_true", help="Print a stable evaluation record.")
    from dynamic_firm.application.evaluation_cli_parser import add_evaluation_commands

    add_evaluation_commands(commands)
    doctor = commands.add_parser(
        "doctor",
        help="Check local configuration and provider readiness without making a request.",
    )
    doctor.add_argument("--json", action="store_true", help="Print a stable diagnostic record.")
    return parser






def _table(config: dict, name: str) -> dict:
    value = config.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"Configuration section [{name}] must be a TOML table.")
    return value


def _first(*values):
    return next((value for value in values if value is not None), None)
