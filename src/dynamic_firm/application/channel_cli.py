"""Channel command lifecycle adapter composed by the CLI facade."""

from __future__ import annotations

from dynamic_firm.application.cli_component_contract import cli

from dynamic_firm import __version__


async def _current_run_goal(*args, **kwargs):
    """Resolve the facade seam at call time for injected CLI test/runtime ports."""

    from dynamic_firm.cli import run_goal

    return await run_goal(*args, **kwargs)

def _run_channel_command(
    args: argparse.Namespace,
    settings: dict,
    config_path: Path,
    output: TextIO,
    *,
    provider_factory: ProviderFactory,
) -> int:
    if args.channel_command == "configure":
        config = cli.ChannelConfig(
            command=args.channel_command_path,
            args=tuple(args.arg),
            environment_names=tuple(dict.fromkeys(args.environment)),
            timeout_seconds=args.timeout_seconds,
            max_message_bytes=args.max_message_bytes,
        )
        target = cli.write_channel_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.channel_status(config)}
    elif args.channel_command == "disable":
        record = {
            "configuration_changed": cli.remove_channel_settings(config_path),
            "config_path": str(config_path.expanduser().resolve()),
            **cli.channel_status(None),
        }
    elif args.channel_command == "inbox-configure":
        config = cli.InboundChannelConfig(
            source_id=args.source_id,
            command=args.inbox_command_path,
            workspace=args.workspace,
            allowed_senders=tuple(dict.fromkeys(args.allow_sender)),
            args=tuple(args.arg),
            environment_names=tuple(dict.fromkeys(args.environment)),
            max_message_bytes=args.max_message_bytes,
            max_messages_per_run=args.max_messages_per_run,
        )
        target = cli.write_inbound_channel_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.inbound_channel_status(config)}
    elif args.channel_command == "inbox-disable":
        record = {
            "configuration_changed": cli.remove_inbound_channel_settings(config_path),
            "config_path": str(config_path.expanduser().resolve()),
            **cli.inbound_channel_status(None),
        }
    elif args.channel_command == "telegram-configure":
        config = cli.TelegramChannelConfig(
            workspace=args.workspace,
            allowed_senders=tuple(dict.fromkeys(args.allow_sender)),
            token_env=args.token_env,
            api_base_url=args.api_base_url,
            max_message_bytes=args.max_message_bytes,
            max_messages_per_run=args.max_messages_per_run,
            poll_timeout_seconds=args.poll_timeout_seconds,
        )
        target = cli.write_telegram_channel_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.telegram_channel_status(config)}
    elif args.channel_command == "telegram-disable":
        record = {
            "configuration_changed": cli.remove_telegram_channel_settings(config_path),
            "config_path": str(config_path.expanduser().resolve()),
            **cli.telegram_channel_status(None),
        }
    elif args.channel_command == "slack-configure":
        config = cli.SlackChannelConfig(
            channel_id=str(args.channel_id).strip(),
            token_env=str(args.token_env).strip(),
            max_message_bytes=int(args.max_message_bytes),
            timeout_seconds=float(args.timeout_seconds),
        )
        target = cli.write_slack_channel_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.slack_channel_status(config)}
    elif args.channel_command == "slack-disable":
        record = {
            "configuration_changed": cli.remove_slack_channel_settings(config_path),
            "config_path": str(config_path.expanduser().resolve()),
            **cli.slack_channel_status(None),
        }
    elif args.channel_command == "slack-inbox-configure":
        config = cli.SlackInboundConfig(
            workspace=args.workspace,
            allowed_senders=tuple(dict.fromkeys(args.allow_sender)),
            allowed_channels=tuple(dict.fromkeys(args.allow_channel)),
            signing_secret_env=args.signing_secret_env,
            port=args.port,
            request_path=args.request_path,
            max_message_bytes=args.max_message_bytes,
            max_messages_per_run=args.max_messages_per_run,
            timestamp_skew_seconds=args.timestamp_skew_seconds,
        )
        target = cli.write_slack_inbound_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.slack_inbound_status(config)}
    elif args.channel_command == "slack-inbox-disable":
        record = {
            "configuration_changed": cli.remove_slack_inbound_settings(config_path),
            "config_path": str(config_path.expanduser().resolve()),
            **cli.slack_inbound_status(None),
        }
    elif args.channel_command == "discord-configure":
        config = cli.DiscordChannelConfig(webhook_env=str(args.webhook_env).strip(), max_message_bytes=int(args.max_message_bytes), timeout_seconds=float(args.timeout_seconds))
        target = cli.write_discord_channel_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.discord_channel_status(config)}
    elif args.channel_command == "discord-disable":
        record = {"configuration_changed": cli.remove_discord_channel_settings(config_path), "config_path": str(config_path.expanduser().resolve()), **cli.discord_channel_status(None)}
    elif args.channel_command == "discord-inbox-configure":
        config = cli.DiscordInboundConfig(
            workspace=args.workspace,
            allowed_senders=tuple(dict.fromkeys(args.allow_sender)),
            allowed_channels=tuple(dict.fromkeys(args.allow_channel)),
            token_env=args.token_env,
            max_message_bytes=args.max_message_bytes,
            max_messages_per_run=args.max_messages_per_run,
        )
        target = cli.write_discord_inbound_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.discord_inbound_status(config)}
    elif args.channel_command == "discord-inbox-disable":
        record = {"configuration_changed": cli.remove_discord_inbound_settings(config_path), "config_path": str(config_path.expanduser().resolve()), **cli.discord_inbound_status(None)}
    elif args.channel_command == "ntfy-configure":
        config = cli.NtfyChannelConfig(
            topic=str(args.topic).strip(),
            server_url=str(args.server_url).strip(),
            token_env=str(args.token_env).strip() if args.token_env is not None and str(args.token_env).strip() else None,
            max_message_bytes=int(args.max_message_bytes),
            timeout_seconds=float(args.timeout_seconds),
            markdown=bool(args.markdown),
        )
        target = cli.write_ntfy_channel_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.ntfy_channel_status(config)}
    elif args.channel_command == "ntfy-disable":
        record = {"configuration_changed": cli.remove_ntfy_channel_settings(config_path), "config_path": str(config_path.expanduser().resolve()), **cli.ntfy_channel_status(None)}
    elif args.channel_command == "ntfy-inbox-configure":
        config = cli.NtfyInboundConfig(args.workspace, str(args.topic).strip(), str(args.token_env).strip() if args.token_env else None, str(args.server_url).strip(), int(args.max_message_bytes), int(args.max_messages_per_run))
        target = cli.write_ntfy_inbound_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.ntfy_inbound_status(config)}
    elif args.channel_command == "ntfy-inbox-disable":
        record = {"configuration_changed": cli.remove_ntfy_inbound_settings(config_path), "config_path": str(config_path.expanduser().resolve()), **cli.ntfy_inbound_status(None)}
    elif args.channel_command == "email-configure":
        config = cli.EmailChannelConfig(
            sender=str(args.sender).strip(), recipients=tuple(dict.fromkeys(str(item).strip() for item in args.to)),
            smtp_host=str(args.smtp_host).strip(), smtp_port=int(args.smtp_port),
            password_env=str(args.password_env).strip(),
            username_env=str(args.username_env).strip() if args.username_env else None,
            max_message_bytes=int(args.max_message_bytes), timeout_seconds=float(args.timeout_seconds),
        )
        target = cli.write_email_channel_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.email_channel_status(config)}
    elif args.channel_command == "email-disable":
        record = {"configuration_changed": cli.remove_email_channel_settings(config_path), "config_path": str(config_path.expanduser().resolve()), **cli.email_channel_status(None)}
    elif args.channel_command == "email-inbox-configure":
        config = cli.EmailInboundConfig(
            workspace=args.workspace, mailbox=str(args.mailbox).strip(), imap_host=str(args.imap_host).strip(),
            allowed_senders=tuple(dict.fromkeys(str(item).strip() for item in args.allow_sender)), imap_port=int(args.imap_port),
            password_env=str(args.password_env).strip(), username_env=str(args.username_env).strip() if args.username_env else None,
            folder=str(args.folder), max_message_bytes=int(args.max_message_bytes), max_messages_per_run=int(args.max_messages_per_run), timeout_seconds=float(args.timeout_seconds),
        )
        target = cli.write_email_inbound_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.email_inbound_status(config)}
    elif args.channel_command == "email-inbox-disable":
        record = {"configuration_changed": cli.remove_email_inbound_settings(config_path), "config_path": str(config_path.expanduser().resolve()), **cli.email_inbound_status(None)}
    elif args.channel_command == "mattermost-configure":
        config = cli.MattermostChannelConfig(str(args.base_url).strip(), str(args.channel_id).strip(), str(args.token_env).strip(), int(args.max_message_bytes), float(args.timeout_seconds))
        target = cli.write_mattermost_channel_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.mattermost_channel_status(config)}
    elif args.channel_command == "mattermost-disable":
        record = {"configuration_changed": cli.remove_mattermost_channel_settings(config_path), "config_path": str(config_path.expanduser().resolve()), **cli.mattermost_channel_status(None)}
    elif args.channel_command == "mattermost-inbox-configure":
        config = cli.MattermostInboundConfig(
            args.workspace, str(args.base_url).strip(), str(args.channel_id).strip(),
            tuple(dict.fromkeys(str(item).strip() for item in args.allow_sender)), str(args.token_env).strip(),
            int(args.max_message_bytes), int(args.max_messages_per_run), float(args.timeout_seconds),
        )
        target = cli.write_mattermost_inbound_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.mattermost_inbound_status(config)}
    elif args.channel_command == "mattermost-inbox-disable":
        record = {"configuration_changed": cli.remove_mattermost_inbound_settings(config_path), "config_path": str(config_path.expanduser().resolve()), **cli.mattermost_inbound_status(None)}
    elif args.channel_command == "matrix-configure":
        config = cli.MatrixChannelConfig(str(args.homeserver_url).strip(), str(args.room_id).strip(), str(args.token_env).strip(), int(args.max_message_bytes), float(args.timeout_seconds))
        target = cli.write_matrix_channel_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.matrix_channel_status(config)}
    elif args.channel_command == "matrix-disable":
        record = {"configuration_changed": cli.remove_matrix_channel_settings(config_path), "config_path": str(config_path.expanduser().resolve()), **cli.matrix_channel_status(None)}
    elif args.channel_command == "matrix-inbox-configure":
        config = cli.MatrixInboundConfig(
            args.workspace, str(args.homeserver_url).strip(), str(args.room_id).strip(),
            tuple(dict.fromkeys(str(item).strip() for item in args.allow_sender)), str(args.token_env).strip(),
            int(args.max_message_bytes), int(args.max_messages_per_run), float(args.timeout_seconds),
        )
        target = cli.write_matrix_inbound_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.matrix_inbound_status(config)}
    elif args.channel_command == "matrix-inbox-disable":
        record = {"configuration_changed": cli.remove_matrix_inbound_settings(config_path), "config_path": str(config_path.expanduser().resolve()), **cli.matrix_inbound_status(None)}
    elif args.channel_command == "dingtalk-configure":
        config=cli.DingTalkChannelConfig(str(args.webhook_env).strip(),int(args.max_message_bytes),float(args.timeout_seconds));target=cli.write_dingtalk_channel_settings(config_path,config);record={"configuration_changed":True,"config_path":str(target),**cli.dingtalk_channel_status(config)}
    elif args.channel_command == "dingtalk-disable":
        record={"configuration_changed":cli.remove_dingtalk_channel_settings(config_path),"config_path":str(config_path.expanduser().resolve()),**cli.dingtalk_channel_status(None)}
    elif args.channel_command == "teams-configure":
        config = cli.TeamsChannelConfig(str(args.webhook_env).strip(), int(args.max_message_bytes), float(args.timeout_seconds))
        target = cli.write_teams_channel_settings(config_path, config)
        record = {"configuration_changed": True, "config_path": str(target), **cli.teams_channel_status(config)}
    elif args.channel_command == "teams-disable":
        record = {"configuration_changed": cli.remove_teams_channel_settings(config_path), "config_path": str(config_path.expanduser().resolve()), **cli.teams_channel_status(None)}
    else:
        config = cli.channel_config_from_settings(settings)
        if args.channel_command == "inbox-status":
            record = dict(cli.inbound_channel_status(cli.inbound_channel_config_from_settings(settings)))
        elif args.channel_command == "inbox-run":
            if not args.confirm:
                raise ValueError("Inbound channel run requires --confirm because accepted messages can consume provider quota")
            inbound_config = cli.inbound_channel_config_from_settings(settings)
            if inbound_config is None:
                raise ValueError("No user-managed inbound channel is configured")
            maximum_messages = args.max_messages if args.max_messages is not None else inbound_config.max_messages_per_run
            state_path = cli._state_path(args, settings)

            async def dispatch(message: InboundMessage) -> tuple[str, str]:
                run_config = cli._inbound_run_config(message, inbound_config, args, settings)
                provider = provider_factory(cli._provider_config(run_config))
                result = await _current_run_goal(
                    run_config,
                    provider,
                    route=cli.route_interactive_input(message.text).route,
                    roster_snapshot=cli._load_active_roster(run_config),
                )
                return result.job_id, result.status.value

            with cli.InboundMessageStore(cli.inbound_state_path(state_path)) as store:
                result = cli.asyncio.run(
                    cli.consume_inbound_channel(
                        inbound_config,
                        store=store,
                        maximum_seconds=args.max_seconds,
                        maximum_messages=maximum_messages,
                        dispatch=dispatch,
                    )
                )
                record = dict(result.to_dict())
            record["config_path"] = str(config_path.expanduser().resolve())
            record["state_path"] = str(state_path.expanduser().resolve())
        elif args.channel_command == "telegram-status":
            record = dict(cli.telegram_channel_status(cli.telegram_channel_config_from_settings(settings)))
        elif args.channel_command == "telegram-run":
            if not args.confirm:
                raise ValueError("Telegram channel run requires --confirm because accepted messages can consume provider quota and send replies")
            telegram_config = cli.telegram_channel_config_from_settings(settings)
            if telegram_config is None:
                raise ValueError("No Telegram channel is configured")
            state_path = cli._state_path(args, settings)
            maximum_messages = args.max_messages if args.max_messages is not None else telegram_config.max_messages_per_run

            async def dispatch_telegram(message: TelegramInboundMessage) -> tuple[str, str, str]:
                run_config = cli._telegram_run_config(message, telegram_config, args, settings)
                provider = provider_factory(cli._provider_config(run_config))
                result = await _current_run_goal(
                    run_config,
                    provider,
                    route=cli.route_interactive_input(message.text).route,
                    roster_snapshot=cli._load_active_roster(run_config),
                )
                return result.job_id, result.status.value, result.summary

            with cli.TelegramChannelStore(cli.telegram_state_path(state_path)) as store:
                result = cli.asyncio.run(
                    cli.run_telegram_channel(
                        telegram_config,
                        store=store,
                        maximum_seconds=args.max_seconds,
                        maximum_messages=maximum_messages,
                        dispatch=dispatch_telegram,
                    )
                )
                record = {
                    "accepted_count": result.accepted_count,
                    "rejected_count": result.rejected_count,
                    "duplicate_count": result.duplicate_count,
                    "ignored_count": result.ignored_count,
                    "highest_offset": result.highest_offset,
                    "dispatches": [
                        {
                            "update_id": item.update_id,
                            "message_id": item.message_id,
                            "sender_id": item.sender_id,
                            "job_id": item.job_id,
                            "job_status": item.job_status,
                            "outcome": item.outcome,
                            "replied": item.replied,
                        }
                        for item in result.dispatches
                    ],
                }
            record["config_path"] = str(config_path.expanduser().resolve())
            record["state_path"] = str(state_path.expanduser().resolve())
        elif args.channel_command == "slack-status":
            record = dict(cli.slack_channel_status(cli.slack_channel_config_from_settings(settings)))
        elif args.channel_command == "slack-inbox-status":
            record = dict(cli.slack_inbound_status(cli.slack_inbound_config_from_settings(settings)))
        elif args.channel_command == "slack-inbox-run":
            if not args.confirm:
                raise ValueError("Slack inbound run requires --confirm because accepted messages can consume provider quota")
            slack_inbound_config = cli.slack_inbound_config_from_settings(settings)
            if slack_inbound_config is None:
                raise ValueError("No Slack inbound receiver is configured")
            state_path = cli._state_path(args, settings)
            maximum_messages = args.max_messages if args.max_messages is not None else slack_inbound_config.max_messages_per_run

            async def dispatch_slack_inbound(message: SlackInboundMessage) -> tuple[str, str]:
                run_config = cli._slack_inbound_run_config(message, slack_inbound_config, args, settings)
                provider = provider_factory(cli._provider_config(run_config))
                result = await _current_run_goal(
                    run_config,
                    provider,
                    route=cli.route_interactive_input(message.text).route,
                    roster_snapshot=cli._load_active_roster(run_config),
                )
                return result.job_id, result.status.value

            with cli.SlackInboundStore(cli.slack_inbound_state_path(state_path)) as store:
                result = cli.asyncio.run(
                    cli.run_slack_inbound_channel(
                        slack_inbound_config,
                        store=store,
                        maximum_seconds=args.max_seconds,
                        maximum_messages=maximum_messages,
                        dispatch=dispatch_slack_inbound,
                    )
                )
                record = {
                    "accepted_count": result.accepted_count,
                    "duplicate_count": result.duplicate_count,
                    "ignored_count": result.ignored_count,
                    "rejected_request_count": result.rejected_request_count,
                    "bound_port": result.bound_port,
                    "dispatches": [
                        {
                            "event_id": item.event_id,
                            "message_id": item.message_id,
                            "sender_id": item.sender_id,
                            "channel_id": item.channel_id,
                            "job_id": item.job_id,
                            "job_status": item.job_status,
                            "outcome": item.outcome,
                        }
                        for item in result.dispatches
                    ],
                }
            record["config_path"] = str(config_path.expanduser().resolve())
            record["state_path"] = str(state_path.expanduser().resolve())
        elif args.channel_command == "slack-test":
            if not args.confirm:
                raise ValueError("Slack test requires --confirm because it sends an external message")
            slack_config = cli.slack_channel_config_from_settings(settings)
            if slack_config is None:
                raise ValueError("No Slack channel is configured")
            record = dict(cli.deliver_slack_message(slack_config, message=str(args.message)).to_dict())
        elif args.channel_command == "discord-status":
            record = dict(cli.discord_channel_status(cli.discord_channel_config_from_settings(settings)))
        elif args.channel_command == "discord-inbox-status":
            record = dict(cli.discord_inbound_status(cli.discord_inbound_config_from_settings(settings)))
        elif args.channel_command == "discord-inbox-run":
            if not args.confirm:
                raise ValueError("Discord inbound run requires --confirm because accepted messages can consume provider quota")
            discord_inbound_config = cli.discord_inbound_config_from_settings(settings)
            if discord_inbound_config is None:
                raise ValueError("No Discord inbound receiver is configured")
            state_path = cli._state_path(args, settings)
            maximum_messages = args.max_messages if args.max_messages is not None else discord_inbound_config.max_messages_per_run

            async def dispatch_discord_inbound(message: DiscordInboundMessage) -> tuple[str, str]:
                run_config = cli._discord_inbound_run_config(message, discord_inbound_config, args, settings)
                provider = provider_factory(cli._provider_config(run_config))
                result = await _current_run_goal(
                    run_config,
                    provider,
                    route=cli.route_interactive_input(message.text).route,
                    roster_snapshot=cli._load_active_roster(run_config),
                )
                return result.job_id, result.status.value

            with cli.DiscordInboundStore(cli.discord_inbound_state_path(state_path)) as store:
                result = cli.asyncio.run(
                    cli.run_discord_inbound_channel(
                        discord_inbound_config,
                        store=store,
                        maximum_seconds=args.max_seconds,
                        maximum_messages=maximum_messages,
                        dispatch=dispatch_discord_inbound,
                    )
                )
                record = {
                    "accepted_count": result.accepted_count,
                    "duplicate_count": result.duplicate_count,
                    "ignored_count": result.ignored_count,
                    "dispatches": [
                        {
                            "message_id": item.message_id,
                            "sender_id": item.sender_id,
                            "channel_id": item.channel_id,
                            "job_id": item.job_id,
                            "job_status": item.job_status,
                            "outcome": item.outcome,
                        }
                        for item in result.dispatches
                    ],
                }
            record["config_path"] = str(config_path.expanduser().resolve())
            record["state_path"] = str(state_path.expanduser().resolve())
        elif args.channel_command == "discord-test":
            if not args.confirm:
                raise ValueError("Discord test requires --confirm because it sends an external message")
            discord_config = cli.discord_channel_config_from_settings(settings)
            if discord_config is None:
                raise ValueError("No Discord channel is configured")
            record = dict(cli.deliver_discord_message(discord_config, message=str(args.message)).to_dict())
        elif args.channel_command == "ntfy-status":
            record = dict(cli.ntfy_channel_status(cli.ntfy_channel_config_from_settings(settings)))
        elif args.channel_command == "ntfy-test":
            if not args.confirm:
                raise ValueError("ntfy test requires --confirm because it sends an external message")
            ntfy_config = cli.ntfy_channel_config_from_settings(settings)
            if ntfy_config is None:
                raise ValueError("No ntfy channel is configured")
            record = dict(cli.deliver_ntfy_message(ntfy_config, message=str(args.message), title=str(args.title)).to_dict())
        elif args.channel_command == "ntfy-inbox-status":
            record = dict(cli.ntfy_inbound_status(cli.ntfy_inbound_config_from_settings(settings)))
        elif args.channel_command == "ntfy-inbox-run":
            if not args.confirm: raise ValueError("ntfy inbound run requires --confirm because accepted messages can consume provider quota")
            ntfy_inbound = cli.ntfy_inbound_config_from_settings(settings)
            if ntfy_inbound is None: raise ValueError("No ntfy inbound receiver is configured")
            state_path = cli._state_path(args, settings); maximum = args.max_messages if args.max_messages is not None else ntfy_inbound.max_messages_per_run
            async def dispatch_ntfy(message: NtfyInboundMessage) -> tuple[str, str]:
                run_config = cli._ntfy_inbound_run_config(message, ntfy_inbound, args, settings)
                result = await _current_run_goal(run_config, provider_factory(cli._provider_config(run_config)), route=cli.route_interactive_input(message.text).route, roster_snapshot=cli._load_active_roster(run_config))
                return result.job_id, result.status.value
            with cli.InboundMessageStore(cli.inbound_state_path(state_path)) as store:
                result = cli.asyncio.run(cli.run_ntfy_inbound(ntfy_inbound, store=store, maximum_seconds=args.max_seconds, maximum_messages=maximum, dispatch=dispatch_ntfy))
            record = {"accepted_count": result.accepted_count, "duplicate_count": result.duplicate_count, "ignored_count": result.ignored_count, "dispatches": list(result.dispatches), "config_path": str(config_path.expanduser().resolve()), "state_path": str(state_path.expanduser().resolve())}
        elif args.channel_command == "email-status":
            record = dict(cli.email_channel_status(cli.email_channel_config_from_settings(settings)))
        elif args.channel_command == "email-test":
            if not args.confirm:
                raise ValueError("Email test requires --confirm because it sends an external message")
            email_config = cli.email_channel_config_from_settings(settings)
            if email_config is None:
                raise ValueError("No email channel is configured")
            record = dict(cli.deliver_email_message(email_config, subject=str(args.subject), message=str(args.message)).to_dict())
        elif args.channel_command == "email-inbox-status":
            record = dict(cli.email_inbound_status(cli.email_inbound_config_from_settings(settings)))
        elif args.channel_command == "email-inbox-run":
            if not args.confirm:
                raise ValueError("Email inbound run requires --confirm because accepted messages can consume provider quota")
            email_config = cli.email_inbound_config_from_settings(settings)
            if email_config is None:
                raise ValueError("No email inbound receiver is configured")
            state_path = cli._state_path(args, settings); maximum = args.max_messages if args.max_messages is not None else email_config.max_messages_per_run
            async def dispatch_email(message: EmailInboundMessage) -> tuple[str, str]:
                run_config = cli._email_inbound_run_config(message, email_config, args, settings)
                result = await _current_run_goal(run_config, provider_factory(cli._provider_config(run_config)), route=cli.route_interactive_input(message.text).route, roster_snapshot=cli._load_active_roster(run_config))
                return result.job_id, result.status.value
            with cli.InboundMessageStore(cli.inbound_state_path(state_path)) as store:
                result = cli.asyncio.run(cli.run_email_inbound(email_config, store=store, maximum_seconds=args.max_seconds, maximum_messages=maximum, dispatch=dispatch_email))
            record = {"accepted_count": result.accepted_count, "duplicate_count": result.duplicate_count, "ignored_count": result.ignored_count, "dispatches": list(result.dispatches), "config_path": str(config_path.expanduser().resolve()), "state_path": str(state_path.expanduser().resolve())}
        elif args.channel_command == "mattermost-status":
            record = dict(cli.mattermost_channel_status(cli.mattermost_channel_config_from_settings(settings)))
        elif args.channel_command == "mattermost-test":
            if not args.confirm:
                raise ValueError("Mattermost test requires --confirm because it sends an external message")
            mattermost_config = cli.mattermost_channel_config_from_settings(settings)
            if mattermost_config is None:
                raise ValueError("No Mattermost channel is configured")
            record = dict(cli.deliver_mattermost_message(mattermost_config, message=str(args.message)).to_dict())
        elif args.channel_command == "matrix-status":
            record = dict(cli.matrix_channel_status(cli.matrix_channel_config_from_settings(settings)))
        elif args.channel_command == "matrix-test":
            if not args.confirm:
                raise ValueError("Matrix test requires --confirm because it sends an external message")
            matrix_config = cli.matrix_channel_config_from_settings(settings)
            if matrix_config is None:
                raise ValueError("No Matrix channel is configured")
            record = dict(cli.deliver_matrix_message(matrix_config, message=str(args.message)).to_dict())
        elif args.channel_command == "matrix-inbox-status":
            record = dict(cli.matrix_inbound_status(cli.matrix_inbound_config_from_settings(settings)))
        elif args.channel_command == "mattermost-inbox-status":
            record = dict(cli.mattermost_inbound_status(cli.mattermost_inbound_config_from_settings(settings)))
        elif args.channel_command == "mattermost-inbox-run":
            if not args.confirm:
                raise ValueError("Mattermost inbound run requires --confirm because accepted messages can consume provider quota")
            mattermost_inbound = cli.mattermost_inbound_config_from_settings(settings)
            if mattermost_inbound is None:
                raise ValueError("No Mattermost inbound receiver is configured")
            state_path = cli._state_path(args, settings)
            maximum = args.max_messages if args.max_messages is not None else mattermost_inbound.max_messages_per_run
            async def dispatch_mattermost(message: MattermostInboundMessage) -> tuple[str, str]:
                run_config = cli._mattermost_inbound_run_config(message, mattermost_inbound, args, settings)
                result = await _current_run_goal(run_config, provider_factory(cli._provider_config(run_config)), route=cli.route_interactive_input(message.text).route, roster_snapshot=cli._load_active_roster(run_config))
                return result.job_id, result.status.value
            with cli.MattermostInboundCursorStore(cli.mattermost_inbound_state_path(state_path)) as cursor_store, cli.InboundMessageStore(cli.inbound_state_path(state_path)) as message_store:
                result = cli.asyncio.run(cli.run_mattermost_inbound(mattermost_inbound, cursor_store=cursor_store, message_store=message_store, dispatch=dispatch_mattermost, maximum_messages=maximum))
            record = {**result.to_dict(), "config_path": str(config_path.expanduser().resolve()), "state_path": str(state_path.expanduser().resolve())}
        elif args.channel_command == "matrix-inbox-run":
            if not args.confirm:
                raise ValueError("Matrix inbound run requires --confirm because accepted messages can consume provider quota")
            matrix_inbound = cli.matrix_inbound_config_from_settings(settings)
            if matrix_inbound is None:
                raise ValueError("No Matrix inbound receiver is configured")
            state_path = cli._state_path(args, settings)
            maximum = args.max_messages if args.max_messages is not None else matrix_inbound.max_messages_per_run
            async def dispatch_matrix(message: MatrixInboundMessage) -> tuple[str, str]:
                run_config = cli._matrix_inbound_run_config(message, matrix_inbound, args, settings)
                result = await _current_run_goal(run_config, provider_factory(cli._provider_config(run_config)), route=cli.route_interactive_input(message.text).route, roster_snapshot=cli._load_active_roster(run_config))
                return result.job_id, result.status.value
            with cli.MatrixInboundCursorStore(cli.matrix_inbound_state_path(state_path)) as cursor_store, cli.InboundMessageStore(cli.inbound_state_path(state_path)) as message_store:
                result = cli.asyncio.run(cli.run_matrix_inbound(matrix_inbound, cursor_store=cursor_store, message_store=message_store, dispatch=dispatch_matrix, maximum_messages=maximum))
            record = {**result.to_dict(), "config_path": str(config_path.expanduser().resolve()), "state_path": str(state_path.expanduser().resolve())}
        elif args.channel_command == "dingtalk-status":
            record=dict(cli.dingtalk_channel_status(cli.dingtalk_channel_config_from_settings(settings)))
        elif args.channel_command == "dingtalk-test":
            if not args.confirm: raise ValueError("DingTalk test requires --confirm because it sends an external message")
            dingtalk_config=cli.dingtalk_channel_config_from_settings(settings)
            if dingtalk_config is None: raise ValueError("No DingTalk channel is configured")
            record=dict(cli.deliver_dingtalk_message(dingtalk_config,title=str(args.title),message=str(args.message)).to_dict())
        elif args.channel_command == "teams-status":
            record = dict(cli.teams_channel_status(cli.teams_channel_config_from_settings(settings)))
        elif args.channel_command == "teams-test":
            if not args.confirm:
                raise ValueError("Teams test requires --confirm because it sends an external message")
            teams_config = cli.teams_channel_config_from_settings(settings)
            if teams_config is None:
                raise ValueError("No Teams channel is configured")
            record = dict(cli.deliver_teams_message(teams_config, message=str(args.message)).to_dict())
        elif args.channel_command == "test":
            if not args.confirm:
                raise ValueError("Channel test delivery requires --confirm")
            if config is None:
                raise ValueError("No user-managed channel is configured")
            record = dict(cli.deliver_channel_test(config, message=args.message, title=args.title).to_dict())
        elif args.channel_command == "job-summary":
            if not args.confirm:
                raise ValueError("Terminal Job summary delivery requires --confirm")
            if config is None:
                raise ValueError("No user-managed channel is configured")
            state_path = cli._state_path(args, settings)
            if not state_path.is_file():
                raise ValueError(f"Unknown ACTIVE JOB: {args.job_id}")
            store = cli.RunStore(state_path)
            try:
                try:
                    inspection = cli.ActiveJobInspector(store).inspect(args.job_id)
                except KeyError as exc:
                    raise ValueError(str(exc).strip("'")) from None
            finally:
                store.close()
            if inspection.audit_status.value != "TERMINAL" or inspection.job_status is None:
                raise ValueError("Only a terminal audited ACTIVE JOB may be delivered to a channel")
            record = dict(
                cli.deliver_terminal_job_summary(
                    config,
                    summary=cli.ChannelJobSummary(
                        job_id=inspection.job_id,
                        job_status=inspection.job_status,
                        audit_status=inspection.audit_status.value,
                        attempt_count=inspection.attempt_count,
                        mutation_count=inspection.mutation_count,
                        final_graph_version=inspection.final_graph_version,
                    ),
                ).to_dict()
            )
        else:
            record = dict(cli.channel_status(config))
        record["config_path"] = str(config_path.expanduser().resolve())
    if args.json:
        print(cli.json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
    elif args.channel_command in {"test", "job-summary"}:
        noun = "test" if args.channel_command == "test" else "terminal Job summary"
        print(f"Outbound channel {noun}: {'delivered' if record['delivered'] else 'failed'}", file=output)
        print("Automatic Job delivery remains disabled; this was one operator-confirmed delivery.", file=output)
        if record["output"]:
            print(f"Result: {record['output']}", file=output)
    elif args.channel_command == "inbox-run":
        print(
            f"Inbound channel foreground run · {record['accepted_count']} accepted · "
            f"{record['duplicate_count']} duplicate · {record['rejected_count']} rejected",
            file=output,
        )
        for item in record["dispatches"]:
            assert isinstance(item, dict)
            detail = f" · {item['job_id']} · {item['job_status']}" if item.get("job_id") else ""
            print(f"  {item['message_id']} · {item['outcome']}{detail}", file=output)
        print("No detached gateway, automatic restart, reply delivery, or permission escalation was created.", file=output)
        if record["process_output"]:
            print(f"Bridge result: {record['process_output']}", file=output)
    elif args.channel_command == "telegram-run":
        print(
            f"Telegram foreground run · {record['accepted_count']} accepted · "
            f"{record['duplicate_count']} duplicate · {record['ignored_count']} ignored",
            file=output,
        )
        for item in record["dispatches"]:
            assert isinstance(item, dict)
            detail = f" · {item['job_id']} · {item['job_status']}" if item.get("job_id") else ""
            reply = " · replied" if item.get("replied") else ""
            print(f"  {item['message_id']} · {item['outcome']}{detail}{reply}", file=output)
        print("Foreground only · no webhook, detached restart, broad sender access, attachment access, or permission escalation.", file=output)
    elif args.channel_command == "slack-inbox-run":
        print(
            f"Slack inbound foreground run · {record['accepted_count']} accepted · "
            f"{record['duplicate_count']} duplicate · {record['ignored_count']} ignored · "
            f"{record['rejected_request_count']} rejected",
            file=output,
        )
        print(f"Loopback receiver: 127.0.0.1:{record['bound_port']} · use only an operator-owned HTTPS proxy for public Slack delivery.", file=output)
        for item in record["dispatches"]:
            assert isinstance(item, dict)
            detail = f" · {item['job_id']} · {item['job_status']}" if item.get("job_id") else ""
            print(f"  {item['event_id']} · {item['outcome']}{detail}", file=output)
        print("Foreground only · signed text events only · no reply, attachment, webhook hosting, detached restart, or permission escalation.", file=output)
    elif args.channel_command in {"slack-test", "discord-test", "ntfy-test", "email-test", "mattermost-test", "matrix-test", "dingtalk-test", "teams-test"}:
        label = {"slack-test": "Slack", "discord-test": "Discord", "ntfy-test": "ntfy", "email-test": "Email", "mattermost-test": "Mattermost", "matrix-test": "Matrix", "dingtalk-test": "DingTalk", "teams-test": "Teams"}[args.channel_command]
        print(f"{label} outbound message: {'delivered' if record['delivered'] else 'failed'}", file=output)
        print("Automatic delivery remains disabled; this was one operator-confirmed delivery.", file=output)
        if record["output"]:
            print(f"Result: {record['output']}", file=output)
    elif args.channel_command in {"slack-status", "slack-configure", "slack-disable"}:
        if not record.get("enabled", False):
            print("Slack channel: disabled", file=output)
            print("Configure it with: noruct channel slack-configure --channel-id CHANNEL_ID", file=output)
        else:
            print(f"Slack channel: {'ready' if record.get('ready') else 'needs token environment'} · operator-confirmed outbound only", file=output)
            if record.get("next_action"):
                print(f"Next: {record['next_action']}", file=output)
    elif args.channel_command in {"slack-inbox-status", "slack-inbox-configure", "slack-inbox-disable"}:
        if not record.get("enabled", False):
            print("Slack inbound receiver: disabled", file=output)
            print("Configure it with: noruct channel slack-inbox-configure --workspace /absolute/workspace --allow-sender U123 --allow-channel C123", file=output)
        else:
            print(f"Slack inbound receiver: {'ready' if record.get('ready') else 'needs signing-secret environment'} · foreground signed events only", file=output)
            print(f"Loopback target: {record['loopback_url']}", file=output)
            if record.get("next_action"):
                print(f"Next: {record['next_action']}", file=output)
    elif args.channel_command in {"discord-status", "discord-configure", "discord-disable"}:
        if not record.get("enabled", False):
            print("Discord channel: disabled", file=output)
            print("Configure it with: noruct channel discord-configure", file=output)
        else:
            print(f"Discord channel: {'ready' if record.get('ready') else 'needs webhook environment'} · operator-confirmed outbound only", file=output)
            if record.get("next_action"):
                print(f"Next: {record['next_action']}", file=output)
    elif args.channel_command in {"discord-inbox-status", "discord-inbox-configure", "discord-inbox-disable"}:
        if not record.get("enabled", False):
            print("Discord inbound receiver: disabled", file=output)
            print("Configure it with: noruct channel discord-inbox-configure --workspace /absolute/workspace --allow-sender DISCORD_NUMERIC_ID --allow-channel DISCORD_NUMERIC_ID", file=output)
        else:
            print(f"Discord inbound receiver: {'ready' if record.get('ready') else 'needs discord.py or token environment'} · foreground text only", file=output)
            if record.get("next_action"):
                print(f"Next: {record['next_action']}", file=output)
    elif args.channel_command in {"ntfy-status", "ntfy-configure", "ntfy-disable"}:
        if not record.get("enabled", False):
            print("ntfy channel: disabled", file=output)
            print("Configure it with: noruct channel ntfy-configure --topic PRIVATE_TOPIC", file=output)
        else:
            state = "ready" if record.get("ready") else "needs token environment"
            print(f"ntfy channel: {state} · operator-confirmed outbound publish only", file=output)
            if record.get("next_action"):
                print(f"Next: {record['next_action']}", file=output)
    elif args.channel_command in {"ntfy-inbox-status", "ntfy-inbox-configure", "ntfy-inbox-disable"}:
        if not record.get("enabled", False): print("ntfy inbound receiver: disabled", file=output)
        else: print(f"ntfy inbound receiver: {'ready' if record.get('ready') else 'needs token environment'} · foreground topic stream only", file=output)
    elif args.channel_command in {"email-status", "email-configure", "email-disable"}:
        if not record.get("enabled", False):
            print("Email channel: disabled", file=output)
            print("Configure it with: noruct channel email-configure --sender SENDER --to RECIPIENT --smtp-host HOST", file=output)
        else:
            print(f"Email channel: {'ready' if record.get('ready') else 'needs credential environment'} · operator-confirmed allowlisted outbound only", file=output)
            if record.get("next_action"):
                print(f"Next: {record['next_action']}", file=output)
    elif args.channel_command in {"email-inbox-status", "email-inbox-configure", "email-inbox-disable"}:
        if not record.get("enabled", False):
            print("Email inbound receiver: disabled", file=output)
            print("Configure it with: noruct channel email-inbox-configure --workspace PATH --mailbox ADDRESS --imap-host HOST --allow-sender ADDRESS", file=output)
        else:
            print(f"Email inbound receiver: {'ready' if record.get('ready') else 'needs credential environment'} · foreground allowlisted plaintext only", file=output)
            if record.get("next_action"):
                print(f"Next: {record['next_action']}", file=output)
    elif args.channel_command == "email-inbox-run":
        print(f"Email inbound foreground run · {record['accepted_count']} accepted · {record['duplicate_count']} duplicate · {record['ignored_count']} ignored", file=output)
        for item in record["dispatches"]:
            detail = f" · {item['job_id']} · {item['job_status']}" if item.get("job_id") else ""
            print(f"  {item['message_id']} · {item['outcome']}{detail}", file=output)
        print("Foreground only · allowlisted plaintext only · no reply, attachment, detached restart, or permission escalation.", file=output)
    elif args.channel_command == "matrix-inbox-run":
        mode = "cursor primed; historical events were not dispatched" if record.get("primed") else "foreground sync batch complete"
        print(f"Matrix inbound {mode} · {record['accepted_count']} accepted · {record['duplicate_count']} duplicate · {record['ignored_count']} ignored", file=output)
        for item in record["dispatches"]:
            detail = f" · {item['job_id']} · {item['job_status']}" if item.get("job_id") else ""
            print(f"  {item['event_id']} · {item['outcome']}{detail}", file=output)
        print("Foreground only · one room and sender allowlist · plaintext m.text only · no reply, E2EE, history, gateway, or detached restart.", file=output)
    elif args.channel_command == "mattermost-inbox-run":
        mode = "cursor primed; historical posts were not dispatched" if record.get("primed") else "foreground post batch complete"
        print(f"Mattermost inbound {mode} · {record['accepted_count']} accepted · {record['duplicate_count']} duplicate · {record['ignored_count']} ignored", file=output)
        for item in record["dispatches"]:
            detail = f" · {item['job_id']} · {item['job_status']}" if item.get("job_id") else ""
            print(f"  {item['post_id']} · {item['outcome']}{detail}", file=output)
        print("Foreground only · one channel and sender allowlist · plaintext posts only · no reply, files, WebSocket, gateway, or detached restart.", file=output)
    elif args.channel_command in {"mattermost-inbox-status", "mattermost-inbox-configure", "mattermost-inbox-disable"}:
        if not record.get("enabled", False):
            print("Mattermost inbound receiver: disabled", file=output)
            print("Configure it with: noruct channel mattermost-inbox-configure --workspace PATH --base-url HTTPS_URL --channel-id ID --allow-sender USER_ID", file=output)
        else:
            print(f"Mattermost inbound receiver: {'ready' if record.get('ready') else 'needs token environment'} · foreground allowlisted plaintext only", file=output)
            if record.get("next_action"):
                print(f"Next: {record['next_action']}", file=output)
    elif args.channel_command in {"mattermost-status", "mattermost-configure", "mattermost-disable"}:
        if not record.get("enabled", False):
            print("Mattermost channel: disabled", file=output)
            print("Configure it with: noruct channel mattermost-configure --base-url HTTPS_URL --channel-id CHANNEL_ID", file=output)
        else:
            print(f"Mattermost channel: {'ready' if record.get('ready') else 'needs token environment'} · operator-confirmed outbound only", file=output)
            if record.get("next_action"):
                print(f"Next: {record['next_action']}", file=output)
    elif args.channel_command in {"matrix-status", "matrix-configure", "matrix-disable"}:
        if not record.get("enabled", False):
            print("Matrix channel: disabled", file=output)
            print("Configure it with: noruct channel matrix-configure --homeserver-url HTTPS_URL --room-id !ROOM:SERVER", file=output)
        else:
            print(f"Matrix channel: {'ready' if record.get('ready') else 'needs token environment'} · operator-confirmed plaintext outbound only", file=output)
            if record.get("next_action"):
                print(f"Next: {record['next_action']}", file=output)
    elif args.channel_command in {"dingtalk-status", "dingtalk-configure", "dingtalk-disable"}:
        if not record.get("enabled",False):
            print("DingTalk channel: disabled",file=output);print("Configure it with: noruct channel dingtalk-configure",file=output)
        else:
            print(f"DingTalk channel: {'ready' if record.get('ready') else 'needs webhook environment'} · operator-confirmed outbound only",file=output)
            if record.get("next_action"):print(f"Next: {record['next_action']}",file=output)
    elif args.channel_command in {"teams-status", "teams-configure", "teams-disable"}:
        if not record.get("enabled", False):
            print("Teams channel: disabled", file=output); print("Configure it with: noruct channel teams-configure", file=output)
        else:
            print(f"Teams channel: {'ready' if record.get('ready') else 'needs webhook environment'} · operator-confirmed outbound only", file=output)
            if record.get("next_action"): print(f"Next: {record['next_action']}", file=output)
    elif args.channel_command == "discord-inbox-run":
        print(
            f"Discord inbound foreground run · {record['accepted_count']} accepted · "
            f"{record['duplicate_count']} duplicate · {record['ignored_count']} ignored",
            file=output,
        )
        for item in record["dispatches"]:
            assert isinstance(item, dict)
            detail = f" · {item['job_id']} · {item['job_status']}" if item.get("job_id") else ""
            print(f"  {item['message_id']} · {item['outcome']}{detail}", file=output)
        print("Foreground only · allowlisted text only · no reply, media, slash command, detached restart, or permission escalation.", file=output)
    elif args.channel_command in {"telegram-status", "telegram-configure", "telegram-disable"}:
        if not record.get("enabled", False):
            print("Telegram channel: disabled", file=output)
            print("Configure it with: noruct channel telegram-configure --workspace /absolute/workspace --allow-sender TELEGRAM_NUMERIC_ID", file=output)
        else:
            print(f"Telegram channel: {'ready' if record.get('ready') else 'needs token environment'} · foreground long-poll only", file=output)
            if record.get("next_action"):
                print(f"Next: {record['next_action']}", file=output)
    elif args.channel_command in {"inbox-status", "inbox-configure", "inbox-disable"}:
        if not record.get("enabled", False):
            print("Inbound channel: disabled", file=output)
            print("Configure it with: noruct channel inbox-configure --source-id bridge --command /absolute/receiver --workspace /absolute/workspace --allow-sender sender", file=output)
        else:
            print(f"Inbound channel: {'ready' if record.get('ready') else 'needs environment'} · foreground operator-confirmed only", file=output)
            if record.get("next_action"):
                print(f"Next: {record['next_action']}", file=output)
    elif not record.get("enabled", record.get("delivered", False)):
        print("Outbound channel: disabled", file=output)
        print("Configure it with: noruct channel configure --command /absolute/sender", file=output)
    else:
        print(f"Outbound channel: {'ready' if record.get('ready') else 'needs environment'} · automatic delivery disabled", file=output)
        if record.get("next_action"):
            print(f"Next: {record['next_action']}", file=output)
    if args.channel_command == "inbox-run":
        failed = any(
            item.get("outcome") == "dispatch_failed" or item.get("job_status") not in {None, "SUCCEEDED"}
            for item in record["dispatches"]
        )
        return cli.EXIT_JOB_FAILED if failed else cli.EXIT_OK
    if args.channel_command == "telegram-run":
        failed = any(item.get("outcome") == "FAILED" or item.get("job_status") not in {None, "SUCCEEDED"} for item in record["dispatches"])
        return cli.EXIT_JOB_FAILED if failed else cli.EXIT_OK
    if args.channel_command == "slack-inbox-run":
        failed = any(item.get("outcome") == "FAILED" or item.get("job_status") not in {None, "SUCCEEDED"} for item in record["dispatches"])
        return cli.EXIT_JOB_FAILED if failed else cli.EXIT_OK
    if args.channel_command == "discord-inbox-run":
        failed = any(item.get("outcome") == "FAILED" or item.get("job_status") not in {None, "SUCCEEDED"} for item in record["dispatches"])
        return cli.EXIT_JOB_FAILED if failed else cli.EXIT_OK
    if args.channel_command == "email-inbox-run":
        failed = any(item.get("outcome") == "FAILED" or item.get("job_status") not in {None, "SUCCEEDED"} for item in record["dispatches"])
        return cli.EXIT_JOB_FAILED if failed else cli.EXIT_OK
    if args.channel_command == "matrix-inbox-run":
        failed = any(item.get("outcome") == "FAILED" or item.get("job_status") not in {None, "SUCCEEDED"} for item in record["dispatches"])
        return cli.EXIT_JOB_FAILED if failed else cli.EXIT_OK
    if args.channel_command in {
        "configure", "disable", "inbox-configure", "inbox-disable",
        "telegram-configure", "telegram-disable", "slack-configure", "slack-disable", "slack-inbox-configure", "slack-inbox-disable", "discord-configure", "discord-disable", "discord-inbox-configure", "discord-inbox-disable", "ntfy-configure", "ntfy-disable", "ntfy-inbox-configure", "ntfy-inbox-disable", "email-configure", "email-disable", "email-inbox-configure", "email-inbox-disable", "mattermost-configure", "mattermost-disable", "mattermost-inbox-configure", "mattermost-inbox-disable", "matrix-configure", "matrix-disable", "matrix-inbox-configure", "matrix-inbox-disable", "dingtalk-configure", "dingtalk-disable", "teams-configure", "teams-disable",
    }:
        return cli.EXIT_OK
    return cli.EXIT_OK if record.get("delivered", record.get("ready", not record.get("enabled", False))) else cli.EXIT_INPUT
