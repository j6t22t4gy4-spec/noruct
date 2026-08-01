"""Command configuration to ActionPolicy assembly."""

from __future__ import annotations

from dynamic_firm.application.cli_component_contract import cli

def _action_policy(
    config: RunCommandConfig,
    *,
    workspace_access: bool = True,
    session_key: str = "",
    manager_tools_enabled: bool = False,
) -> cli.ActionPolicy:
    manager_grants = (
        (
            cli.ToolGrant(
                tool_name="manager_inspect_company",
                allowed_effects=(cli.ToolEffect.READ,),
                resource_patterns=("manager:company",),
                max_calls=min(2, config.run_limits.max_tool_calls),
            ),
            cli.ToolGrant(
                tool_name="manager_inspect_current_job",
                allowed_effects=(cli.ToolEffect.READ,),
                resource_patterns=("manager:job:*",),
                max_calls=min(3, config.run_limits.max_tool_calls),
            ),
            cli.ToolGrant(
                tool_name="manager_read_intent_brief",
                allowed_effects=(cli.ToolEffect.READ,),
                resource_patterns=("manager:intent",),
                max_calls=min(2, config.run_limits.max_tool_calls),
            ),
            cli.ToolGrant(
                tool_name="manager_review_recent_outcomes",
                allowed_effects=(cli.ToolEffect.READ,),
                resource_patterns=("manager:outcomes",),
                max_calls=min(2, config.run_limits.max_tool_calls),
            ),
        )
        if manager_tools_enabled
        else ()
    )
    if not workspace_access:
        # A direct conversation in ask mode deliberately receives the same
        # bounded local-tool contract as a Company Job.  This branch is for
        # read-only direct answers only: it prevents unsolicited inspection,
        # while the router still remains free to select the cheaper direct
        # agent loop instead of a Company graph.
        return cli.ActionPolicy(
            tool_grants=manager_grants,
            filesystem_policy="DENY",
            sandbox_profile="none",
        )
    settings_grants = [
        cli.ToolGrant(
            tool_name="inspect_global_settings",
            allowed_effects=(cli.ToolEffect.READ,),
            resource_patterns=("noruct:settings:global",),
            max_calls=min(4, config.run_limits.max_tool_calls),
        ),
    ]
    # The action-policy schema has one filesystem write class. A Company
    # employee can therefore change global defaults only in the same explicit
    # interactive authority posture that permits a real workspace mutation.
    # Read-only jobs may still inspect the entire redacted Settings Center.
    if config.permission_mode == "ask" and config.agent_settings_mode == "ask":
        settings_grants.append(
            cli.ToolGrant(
                tool_name="apply_global_setting",
                allowed_effects=(cli.ToolEffect.WRITE,),
                resource_patterns=("noruct:settings:global:*",),
                max_calls=min(2, config.run_limits.max_tool_calls),
                requires_approval=True,
            )
        )
    settings_grants = tuple(settings_grants)
    grants = [
        *settings_grants,
        cli.ToolGrant(
            tool_name="knowledge_recall",
            allowed_effects=(cli.ToolEffect.READ,),
            resource_patterns=("knowledge:recall:*",),
            max_calls=min(6, config.run_limits.max_tool_calls),
        ),
        cli.ToolGrant(
            tool_name="knowledge_folder_open",
            allowed_effects=(cli.ToolEffect.READ,),
            resource_patterns=("knowledge:folder-open:*",),
            max_calls=min(6, config.run_limits.max_tool_calls),
        ),
        cli.ToolGrant(
            tool_name="list_workspace_files",
            allowed_effects=(cli.ToolEffect.READ,),
            resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:*",),
            max_calls=config.run_limits.max_tool_calls,
        ),
        cli.ToolGrant(
            tool_name="read_workspace_file",
            allowed_effects=(cli.ToolEffect.READ,),
            resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:*",),
            max_calls=config.run_limits.max_tool_calls,
        ),
        cli.ToolGrant(
            tool_name="search_workspace_files",
            allowed_effects=(cli.ToolEffect.READ,),
            resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:*",),
            max_calls=config.run_limits.max_tool_calls,
        ),
    ]
    # A compatible SKILL.md package is user-configured local context.  Its
    # support files are exposed only after the normal per-Job selector froze
    # the small eligible set, but the immutable ActionPolicy must reserve the
    # read grant before the Employee runtime is constructed.
    if config.external_skill_dirs:
        grants.append(
            cli.ToolGrant(
                tool_name=cli.ExternalSkillPackageTools.tool_name,
                allowed_effects=(cli.ToolEffect.READ,),
                resource_patterns=("skill-package:*",),
                max_calls=min(8, config.run_limits.max_tool_calls),
            )
        )
    grants.extend(manager_grants)
    # Session-recall tools require the caller-owned session namespace that is
    # registered by ``run_goal``.  Do not issue grants for a tool family that
    # this immutable Job cannot actually receive.
    if session_key:
        grants.extend((
            cli.ToolGrant(
                tool_name="search_company_session_memory",
                allowed_effects=(cli.ToolEffect.READ,),
                resource_patterns=("company:session:catalog",),
                max_calls=min(8, config.run_limits.max_tool_calls),
            ),
            cli.ToolGrant(
                tool_name="read_company_session_memory",
                allowed_effects=(cli.ToolEffect.READ,),
                resource_patterns=("company:session:*",),
                max_calls=min(8, config.run_limits.max_tool_calls),
            ),
        ))
    # `blocked` is a real deny posture, not merely a label in Settings.  It
    # leaves local read/write policy intact but withholds every tool whose
    # purpose is an external or device state transition.  `ask` keeps the
    # dialog. `user-authorized-auto` is interpreted by the frozen capability
    # trust projection below rather than being a misleading no-op setting.
    external_actions_enabled = config.external_state_mode != "blocked"
    external_reads_enabled = config.external_read_mode != "blocked"
    external_reads_require_approval = config.external_read_mode == "ask"
    if config.mcp_read_only is not None and external_reads_enabled:
        runtime_tools = config.mcp_read_only.selected_runtime_tool_names()
        for tool_name in runtime_tools:
            profile = config.mcp_read_only.profile_for_runtime_tool(tool_name)
            resource = (
                f"external-read:{profile}"
                if len(runtime_tools) == 1
                else f"external-read:{profile}:{tool_name}"
            )
            grants.append(
                cli.ToolGrant(
                    tool_name=tool_name,
                    allowed_effects=(cli.ToolEffect.NETWORK,),
                    resource_patterns=(resource,),
                    max_calls=1,
                    requires_approval=external_reads_require_approval,
                )
            )
    if config.browser_read_only is not None and external_reads_enabled:
        grants.append(
            cli.ToolGrant(
                tool_name="list_browser_tabs",
                allowed_effects=(cli.ToolEffect.READ,),
                resource_patterns=("browser:local:tabs",),
                max_calls=min(4, config.run_limits.max_tool_calls),
                requires_approval=external_reads_require_approval,
            )
        )
        # Reading a configured existing tab is an external-read decision, not
        # a workspace mutation.  The old condition hid it in read-only
        # conversations even when external reads were explicitly allowed.
        grants.append(
            cli.ToolGrant(
                tool_name="read_browser_page",
                allowed_effects=(cli.ToolEffect.READ,),
                resource_patterns=("browser:local:tab:*",),
                max_calls=min(4, config.run_limits.max_tool_calls),
                requires_approval=external_reads_require_approval,
            )
        )
        if (
            config.permission_mode == "ask"
            and config.browser_read_only.allow_control
            and external_actions_enabled
        ):
            for tool_name, action in (
                ("navigate_browser_tab", "navigate"),
                ("click_browser_element", "click"),
                ("type_browser_text", "type"),
            ):
                grants.append(
                    cli.ToolGrant(
                        tool_name=tool_name,
                        allowed_effects=(cli.ToolEffect.EXECUTE,),
                        resource_patterns=(f"browser:local:tab:*:{action}",),
                        max_calls=min(2, config.run_limits.max_tool_calls),
                        requires_approval=True,
                    )
                )
            if config.browser_read_only.capture_directory is not None:
                grants.append(
                    cli.ToolGrant(
                        tool_name="capture_browser_screenshot",
                        allowed_effects=(cli.ToolEffect.EXECUTE,),
                        resource_patterns=("browser:local:tab:*:capture",),
                        max_calls=1,
                        requires_approval=True,
                    )
                )
    if config.computer_use is not None and config.permission_mode == "ask" and external_actions_enabled:
        grants.append(
            cli.ToolGrant(
                tool_name="computer_use",
                allowed_effects=(cli.ToolEffect.EXECUTE,),
                resource_patterns=("computer:local:*",),
                max_calls=min(8, config.run_limits.max_tool_calls),
                requires_approval=True,
            )
        )
    if config.openai_media is not None and config.permission_mode == "ask" and external_actions_enabled:
        for capability in config.openai_media.enabled_capabilities:
            tool_name = {
                "image": "generate_image",
                "speech": "synthesize_speech",
                "transcription": "transcribe_audio",
                "video": "generate_video",
            }[capability]
            grants.append(
                cli.ToolGrant(
                    tool_name=tool_name,
                    allowed_effects=(cli.ToolEffect.NETWORK,),
                    resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:media:{capability}:*",),
                    max_calls=1,
                    requires_approval=True,
                )
            )
    if config.mcp_action is not None and config.permission_mode == "ask" and external_actions_enabled:
        for action, runtime_tool_name in zip(
            cli.mcp_action_configs(config.mcp_action),
            cli.mcp_action_runtime_tool_names(config.mcp_action),
            strict=True,
        ):
            grants.append(
                cli.ToolGrant(
                    tool_name=runtime_tool_name,
                    allowed_effects=(cli.ToolEffect.EXECUTE,),
                    resource_patterns=(f"external-action:{action.profile}",),
                    max_calls=1,
                    requires_approval=True,
                )
            )
    if config.web_read is not None and external_reads_enabled:
        grants.append(
            cli.ToolGrant(
                tool_name=cli.WEB_READ_TOOL,
                allowed_effects=(cli.ToolEffect.NETWORK,),
                resource_patterns=("external-read:web-page",),
                max_calls=1,
                requires_approval=external_reads_require_approval,
            )
        )
    if config.web_search is not None and external_reads_enabled:
        grants.append(
            cli.ToolGrant(
                tool_name=cli.WEB_SEARCH_TOOL,
                allowed_effects=(cli.ToolEffect.NETWORK,),
                resource_patterns=("external-read:web-search",),
                max_calls=min(3, config.run_limits.max_tool_calls),
                requires_approval=external_reads_require_approval,
            )
        )
    if config.home_assistant is not None:
        for definition in cli.HomeAssistantTools(config.home_assistant).definitions():
            if definition.effect == cli.ToolEffect.EXECUTE and not external_actions_enabled:
                continue
            if definition.effect != cli.ToolEffect.EXECUTE and not external_reads_enabled:
                continue
            grants.append(cli.ToolGrant(
                tool_name=definition.name,
                allowed_effects=(definition.effect,),
                resource_patterns=("home-assistant:*",),
                max_calls=1 if definition.effect == cli.ToolEffect.EXECUTE else min(4, config.run_limits.max_tool_calls),
                requires_approval=(definition.effect == cli.ToolEffect.EXECUTE or external_reads_require_approval),
            ))
    if config.executable_plugins is not None and external_actions_enabled:
        for plugin in config.executable_plugins.plugins:
            for definition in plugin.definitions():
                grants.append(
                    cli.ToolGrant(
                        tool_name=definition.name,
                        allowed_effects=(cli.ToolEffect.EXECUTE,),
                        resource_patterns=(f"plugin:{plugin.plugin_id}:{plugin.version}:{definition.name}",),
                        max_calls=1,
                        requires_approval=True,
                    )
                )
    if config.remote_worker is not None and external_actions_enabled:
        grants.append(cli.ToolGrant(
            tool_name="run_remote_workspace_program", allowed_effects=(cli.ToolEffect.EXECUTE,),
            resource_patterns=(f"remote-workspace:{config.remote_worker.target_id}:*:{config.remote_worker.snapshot_sha256}",),
            max_calls=1, requires_approval=True,
        ))
    if config.container_workspace is not None and external_actions_enabled:
        grants.append(cli.ToolGrant(
            tool_name="run_container_workspace_program", allowed_effects=(cli.ToolEffect.EXECUTE,),
            resource_patterns=(f"container-workspace:{config.container_workspace.image}:*",),
            max_calls=1, requires_approval=True,
        ))
    if config.provider_kind == "openai_codex" and config.permission_mode == "ask":
        grants.append(
            cli.ToolGrant(
                tool_name=cli.APPLY_CHANGE_SET_TOOL,
                allowed_effects=(cli.ToolEffect.WRITE,),
                resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:change-set:*",),
                max_calls=1,
                requires_approval=True,
            )
        )
        # Do not return here.  The native employee's direct file and terminal
        # tools below are also valid in Codex ask mode.  The change-set grant
        # merely enables the optional disposable-workspace lane for broad
        # coding jobs; it is not a replacement for ordinary host operations.
    if config.permission_mode == "ask":
        grants.extend(
            (
                cli.ToolGrant(
                    tool_name="knowledge_remember",
                    allowed_effects=(cli.ToolEffect.WRITE,),
                    resource_patterns=("knowledge:record",),
                    max_calls=min(4, config.run_limits.max_tool_calls),
                    requires_approval=True,
                ),
                cli.ToolGrant(
                    tool_name="knowledge_ingest",
                    allowed_effects=(cli.ToolEffect.WRITE,),
                    resource_patterns=("knowledge:ingest:*",),
                    max_calls=min(4, config.run_limits.max_tool_calls),
                    requires_approval=True,
                ),
                cli.ToolGrant(
                    tool_name="write_workspace_file",
                    allowed_effects=(cli.ToolEffect.WRITE,),
                    resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:*",),
                    max_calls=config.run_limits.max_tool_calls,
                    requires_approval=True,
                ),
                cli.ToolGrant(
                    tool_name="edit_workspace_file",
                    allowed_effects=(cli.ToolEffect.WRITE,),
                    resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:*",),
                    max_calls=config.run_limits.max_tool_calls,
                    requires_approval=True,
                ),
                cli.ToolGrant(
                    tool_name="patch_workspace_file",
                    allowed_effects=(cli.ToolEffect.WRITE,),
                    resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:*",),
                    max_calls=config.run_limits.max_tool_calls,
                    requires_approval=True,
                ),
                cli.ToolGrant(
                    tool_name="apply_workspace_multi_patch",
                    allowed_effects=(cli.ToolEffect.WRITE,),
                    resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:multi-patch:*",),
                    max_calls=config.run_limits.max_tool_calls,
                    requires_approval=True,
                ),
                cli.ToolGrant(
                    tool_name="move_workspace_file",
                    allowed_effects=(cli.ToolEffect.WRITE,),
                    resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:move:*",),
                    max_calls=config.run_limits.max_tool_calls,
                    requires_approval=True,
                ),
                cli.ToolGrant(
                    tool_name="delete_workspace_file",
                    allowed_effects=(cli.ToolEffect.WRITE,),
                    resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:delete:*",),
                    max_calls=config.run_limits.max_tool_calls,
                    requires_approval=True,
                ),
                cli.ToolGrant(
                    tool_name="run_workspace_command",
                    allowed_effects=(cli.ToolEffect.EXECUTE,),
                    resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:command:*",),
                    max_calls=config.run_limits.max_tool_calls,
                    requires_approval=True,
                ),
                cli.ToolGrant(
                    tool_name="run_workspace_background_command",
                    allowed_effects=(cli.ToolEffect.EXECUTE,),
                    resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:process-start:*",),
                    max_calls=min(4, config.run_limits.max_tool_calls),
                    requires_approval=True,
                ),
                cli.ToolGrant(
                    tool_name="list_workspace_processes",
                    allowed_effects=(cli.ToolEffect.READ,),
                    resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:processes",),
                    max_calls=config.run_limits.max_tool_calls,
                ),
                cli.ToolGrant(
                    tool_name="inspect_workspace_process",
                    allowed_effects=(cli.ToolEffect.READ,),
                    resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:process:*",),
                    max_calls=config.run_limits.max_tool_calls,
                ),
                cli.ToolGrant(
                    tool_name="wait_workspace_process",
                    allowed_effects=(cli.ToolEffect.READ,),
                    resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:process:*",),
                    max_calls=config.run_limits.max_tool_calls,
                ),
                cli.ToolGrant(
                    tool_name="write_workspace_process_stdin",
                    allowed_effects=(cli.ToolEffect.EXECUTE,),
                    resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:process:*",),
                    max_calls=min(8, config.run_limits.max_tool_calls),
                    requires_approval=True,
                ),
                cli.ToolGrant(
                    tool_name="stop_workspace_process",
                    allowed_effects=(cli.ToolEffect.EXECUTE,),
                    resource_patterns=(f"workspace:{cli.WORKSPACE_ID}:process:*",),
                    max_calls=min(4, config.run_limits.max_tool_calls),
                    requires_approval=True,
                ),
            )
        )
    return cli.ActionPolicy(
        tool_grants=tuple(grants),
        network_policy=(
            # Derive the network envelope from the grants that were actually
            # emitted, not merely from a configured integration.  For
            # example, a media profile remains stored when Workspace authority
            # is read-only, but no media tool is granted in that posture.  An
            # EXTERNAL_READ_ONLY declaration without any NETWORK grant is
            # invalid at the foundation boundary and previously made every
            # such Job fail before its first model call.
            "EXTERNAL_READ_ONLY"
            if any(cli.ToolEffect.NETWORK in grant.allowed_effects for grant in grants)
            else "DENY"
        ),
        filesystem_policy=(
            "WORKSPACE_WRITE" if config.permission_mode == "ask" else "READ_ONLY"
        ),
        sandbox_profile=(
            (
                "host-workspace-approved"
                if config.mcp_action is not None
                else "remote-and-browser-approved"
                if config.remote_worker is not None and config.browser_read_only is not None and config.browser_read_only.allow_control
                else "remote-workspace-approved"
                if config.remote_worker is not None
                else "computer-and-browser-approved"
                if config.computer_use is not None and config.browser_read_only is not None and config.browser_read_only.allow_control
                else "computer-use-approved"
                if config.computer_use is not None
                else "browser-control-approved"
                if config.browser_read_only is not None and config.browser_read_only.allow_control
                else "executable-plugin-approved"
                if config.executable_plugins is not None and config.executable_plugins.plugins
                else "host-workspace-approved"
            )
            if config.permission_mode == "ask"
            else "none"
        ),
        capability_trust_mode=config.capability_trust_mode,
        auto_approved_tool_names=cli._auto_approved_tool_names(config, grants),
    )
