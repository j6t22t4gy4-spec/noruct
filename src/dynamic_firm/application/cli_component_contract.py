"""Explicit, lazy composition contract for split CLI command families.

The compatibility facade exposes a wide legacy surface, while each command
family needs only a subset.  This object is the one declared dependency path:
it first resolves immutable common services and then a registered component.
It deliberately never mutates another module's globals.
"""

from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType
from typing import Any

from dynamic_firm.application import cli_composition


_COMPONENT_EXPORTS = {
    "action_policy_assembly": ("_action_policy",),
    "channel_cli": ("_current_run_goal", "_run_channel_command"),
    "coding_evaluation_cli": (
        "_current_isatty", "_run_tui_acceptance_evaluation", "_live_coding_config",
        "_run_coding_evaluation",
    ),
    "company_runtime_assembly": (
        "_default_roster", "_load_active_roster", "_interactive_approval_available_for",
        "_has_configured_external_read_capability", "_auto_approved_tool_names",
        "_workspace_manifest", "_company_operating_brief", "_operating_decision_for_route",
        "_compiler_execution_profile", "_company_request", "_emit_product_event",
        "_shadow_exclusions", "_empty_evolution_artifact_resolution",
        "_mcp_policy_for_frozen_artifacts", "_resolve_evolution_artifacts_for_job",
        "_advance_preapproved_evolution_artifacts",
    ),
    "config_cli": (
        "_load_config", "_reject_config_secrets", "_provider_kind",
        "_execution_provider_kind", "_foundation_worker_has_required_profile",
        "_resolve_foundation_runtime_python", "_run_config",
    ),
    "doctor_cli": ("_run_doctor",),
    "entrypoint_cli": ("main",),
    "environment_cli": ("_run_environment_command",),
    "gateway_cli": (
        "_gateway_service_record", "_gateway_service_log_tail", "_gateway_receiver_configs",
        "_gateway_receiver_readiness", "_gateway_safe_config_projection",
        "_gateway_receiver_config_digest", "_gateway_receiver_configuration_status",
        "_run_gateway_service_command", "_run_gateway_command",
    ),
    "goal_runtime": ("run_goal",),
    "graph_cli": (
        "GraphCliPorts", "GraphCommunityPorts", "GraphRegistryPorts", "_require_confirm",
        "run_stateless_graph_command", "run_graph_registry_command",
        "run_graph_community_command", "run_natural_graph_edit_command",
        "_graph_preview_for_config", "_render_graph_control", "_run_graph_command",
    ),
    "inbound_evaluation_cli": (
        "_provider_preflight_config", "_remote_worker_programs", "_container_programs",
        "_inbound_run_config", "_telegram_run_config", "_ntfy_inbound_run_config",
        "_matrix_inbound_run_config", "_mattermost_inbound_run_config",
        "_email_inbound_run_config", "_slack_inbound_run_config", "_discord_inbound_run_config",
        "_run_company_learning_evaluation", "_run_patch_observation_evaluation",
        "_run_roster_patch_evaluation", "_run_hiring_evaluation",
        "_run_hire_observation_evaluation", "_run_retention_review_evaluation",
        "_run_employee_skill_evaluation", "_run_task_mutation_evaluation",
    ),
    "interactive_cli": ("_run_interactive",),
    "interactive_runtime_cli": (
        "_continue_read_only_partial_runtime", "_run_read_only_partial_continuation",
        "_run_read_only_partial_handoff", "_handoff_read_only_partial_runtime",
        "_continue_graph_proposal_runtime", "_run_graph_proposal_continuation", "_run_once",
        "_run_acp_command", "_interactive_help", "_interactive_skill_messages",
        "_session_browse_response", "_activate_interactive_session", "_modern_controller_ports",
        "_ModernInteractiveController", "_run_modern_interactive", "_is_alternate_screen_terminal",
        "_resolve_interactive_terminal_ui",
    ),
    "knowledge_cli_renderer": ("_render_knowledge_human",),
    "product_interaction_cli": (
        "_isatty", "_normalize_argv", "_state_path", "_evolution_state_path",
        "_evolution_human_summary", "_run_evolution", "_run_network", "_run_foundation",
        "_knowledge_paths", "_knowledge_json", "_knowledge_display", "_run_knowledge",
        "_render_intent_human", "_run_intent", "_render_decision_human", "_run_decision",
        "_render_question_human", "_run_question", "_render_research_human", "_run_research",
        "_run_skills_command", "_schedule_ports", "_run_schedule_service_command",
        "_run_schedule_command",
    ),
    "runtime_command_cli": (
        "_goal_execution_services", "_default_provider", "_default_coding_worker",
        "_provider_display", "_authority_display", "_tui_company_facts",
        "_company_settings_entries", "_render_result", "_run_demo", "_write_evaluation_record",
        "_single_provider_config", "_fallback_route_from_text", "_configured_fallback_routes",
        "_configured_moa_reference_routes", "_provider_config", "_prepare_permission_mode",
        "_prompt_value", "_prompt_choice", "_prompt_yes_no", "_has_explicit_setup_transport",
        "_provider_is_ready_without_network", "_needs_first_run_onboarding",
        "_first_run_setup_args", "_run_sessions", "_run_session_command", "_run_job", "_run_company",
        "_run_portfolio",
    ),
    "setup_cli": (
        "_run_capabilities_command", "_run_update_command", "_run_provider_command", "_run_setup",
    ),
}
_COMPONENT_OWNER = {
    name: module_name
    for module_name, exports in _COMPONENT_EXPORTS.items()
    for name in exports
}

_COMPATIBILITY_CONSTANTS = {
    "_PROMPT_VISIBLE_COMPANY_POLICIES": (
        "high_cost_or_irreversible_requires_user_approval",
        "roster_retention_review_mode",
        "evolution_autonomy_mode",
        "company_cost_budget",
    ),
    "_TUI_TOOL_NAMES": {
        "list_workspace_files": "list",
        "read_workspace_file": "read",
        "search_workspace_files": "search",
        "write_workspace_file": "write",
        "edit_workspace_file": "edit",
        "patch_workspace_file": "patch",
        "apply_workspace_multi_patch": "multi-file patch",
        "move_workspace_file": "move",
        "delete_workspace_file": "delete",
        "run_workspace_command": "command",
        "run_workspace_background_command": "background command",
        "list_workspace_processes": "processes",
        "inspect_workspace_process": "process status",
        "wait_workspace_process": "wait process",
        "write_workspace_process_stdin": "process input",
        "stop_workspace_process": "stop process",
        "search_company_session_memory": "session recall",
        "read_company_session_memory": "session memory",
        cli_composition.APPLY_CHANGE_SET_TOOL: "apply change set",
        cli_composition.EXTERNAL_READ_TOOL: "external read",
        cli_composition.WEB_SEARCH_TOOL: "web search",
    },
}


class CliComponentContract:
    """Read-only component registry with test-compatible facade overrides."""

    def __getattr__(self, name: str) -> Any:
        # Compatibility tests and embedding callers may replace an exported
        # facade dependency.  Honor that explicit override before consulting
        # the immutable composition registry.
        facade = sys.modules.get("dynamic_firm.cli")
        if isinstance(facade, ModuleType):
            override = vars(facade).get(name, _MISSING)
            if override is not _MISSING:
                return override
        constant = _COMPATIBILITY_CONSTANTS.get(name, _MISSING)
        if constant is not _MISSING:
            return constant
        common = getattr(cli_composition, name, _MISSING)
        if common is not _MISSING:
            return common
        module_name = _COMPONENT_OWNER.get(name)
        if module_name is not None:
            component = import_module(f"dynamic_firm.application.{module_name}")
            return getattr(component, name)
        raise AttributeError(f"CLI composition has no dependency named {name!r}")


_MISSING = object()
cli = CliComponentContract()
