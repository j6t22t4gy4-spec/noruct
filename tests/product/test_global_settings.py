from __future__ import annotations

import tempfile
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path

from dynamic_firm.product.global_settings import (
    GlobalRuntimeSettings,
    write_global_runtime_settings,
)
from dynamic_firm.product.local_routing_settings import (
    LocalRoutingSettings,
    write_local_routing_settings,
)
from dynamic_firm.product.settings_registry import SettingsRegistry
from dynamic_firm.company.user_routing_policy import (
    ApprovedRouteMetadata,
    ApprovedRouteRegistry,
    UserRoutingPolicy,
    UserRoutingPolicyMode,
)


class GlobalRuntimeSettingsTests(unittest.TestCase):
    def test_registry_projects_local_routing_policy_without_route_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            write_local_routing_settings(
                path,
                LocalRoutingSettings(
                    UserRoutingPolicy(UserRoutingPolicyMode.PRIVATE_LOCAL_FIRST),
                    ApprovedRouteRegistry((ApprovedRouteMetadata(
                        route_id="private-route-1",
                        execution_route_binding_digest="b" * 64,
                        provider_config_digest="a" * 64,
                        credential_reference="ROUTE_CREDENTIAL",
                    ),)),
                ),
            )
            row = {entry.key: entry for entry in SettingsRegistry(path).entries()}["routing.local_policy"]

        self.assertEqual(row.state, "configured")
        self.assertEqual(row.value, "PRIVATE_LOCAL_FIRST · 1 approved routes")
        self.assertNotIn("private-route-1", row.value + row.summary)
        self.assertNotIn("ROUTE_CREDENTIAL", row.value + row.summary)
        self.assertNotIn("a" * 64, row.value + row.summary)

    def test_registry_projects_missing_local_routing_table_as_first_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            write_global_runtime_settings(
                path,
                GlobalRuntimeSettings.from_mapping(
                    {"provider": {"kind": "openai_codex", "codex_command": "codex"}}
                ),
            )
            row = {entry.key: entry for entry in SettingsRegistry(path).entries()}["routing.local_policy"]

        self.assertEqual(row.state, "first-run-no-approved-routes")
        self.assertEqual(row.value, "BALANCED · 0 approved routes")

    def test_registry_fail_closes_malformed_local_routing_table_without_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                "[model_routing]\n"
                "policy = \"{\\\"mode\\\":\\\"SECRET_ROUTE\\\"}\"\n"
                "approved_routes = \"{\\\"routes\\\":[]}\"\n",
                encoding="utf-8",
            )
            row = {entry.key: entry for entry in SettingsRegistry(path).entries()}["routing.local_policy"]

        self.assertEqual(row.state, "configuration-error")
        self.assertEqual(row.value, "configuration error")
        self.assertNotIn("SECRET_ROUTE", row.value + row.summary)

    def test_registry_keeps_every_foundation_capability_lane_visible_before_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            write_global_runtime_settings(
                path,
                GlobalRuntimeSettings.from_mapping(
                    {"provider": {"kind": "openai_codex", "codex_command": "codex"}}
                ),
            )
            rows = {entry.key: entry for entry in SettingsRegistry(path).entries()}

        # A missing config must mean "available with setup", not that the
        # foundation feature disappears from the operator surface.
        self.assertEqual(rows["run.permission_mode"].value, "ask")
        self.assertEqual(rows["run.external_read_mode"].value, "allow")
        self.assertEqual(rows["run.external_state_mode"].value, "ask")
        self.assertEqual(rows["run.agent_settings_mode"].value, "ask")
        self.assertEqual(rows["run.max_wall_time"].value, "86400")
        self.assertEqual(rows["run.max_model_calls"].value, "2048")
        self.assertTrue(
            {
                "integration.mcp",
                "integration.mcp_action",
                "integration.browser",
                "integration.computer_use",
                "integration.web_search",
                "integration.openai_media",
                "integration.plugin",
                "integration.skills",
                "integration.remote_worker",
                "integration.container",
                "channel.telegram_channel",
                "channel.slack_channel",
                "channel.discord_channel",
                "channel.email_channel",
                "channel.ntfy_channel",
                "channel.matrix_channel",
                "channel.mattermost_channel",
                "automation.schedule",
            }.issubset(rows)
        )
        self.assertTrue(all(rows[key].setup_hint for key in (
            "integration.mcp", "integration.browser", "integration.computer_use",
            "integration.plugin", "integration.skills", "integration.remote_worker",
            "integration.container", "channel.telegram_channel", "channel.slack_channel",
            "automation.schedule",
        )))

    def test_rewrites_global_provider_and_run_without_losing_capability_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                "[provider]\nkind = \"openai_codex\"\ncodex_command = \"codex\"\n\n"
                "[run]\nstate = \"old.db\"\npermission_mode = \"read-only\"\n\n"
                "[mcp]\ncommand = \"/usr/bin/python3\"\nargs = [\"server.py\"]\n",
                encoding="utf-8",
            )
            defaults = GlobalRuntimeSettings.from_mapping(tomllib.loads(path.read_text()))
            target = write_global_runtime_settings(
                path,
                replace(defaults, permission_mode="ask", state_path="new.db"),
            )
            stored = tomllib.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(stored["run"]["permission_mode"], "ask")
        self.assertEqual(stored["run"]["state"], "new.db")
        self.assertEqual(stored["mcp"]["command"], "/usr/bin/python3")

    def test_registry_is_redacted_and_agent_change_is_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                "[provider]\nkind = \"openai_codex\"\ncodex_command = \"codex\"\nmodel = \"gpt-test\"\n\n"
                "[run]\nstate = \"runtime.db\"\npermission_mode = \"ask\"\nexternal_state_mode = \"ask\"\n\n"
                "[telegram_channel]\nenabled = true\ntoken_env = \"TELEGRAM_TEST_TOKEN\"\nworkspace = \".\"\n",
                encoding="utf-8",
            )
            registry = SettingsRegistry(path)
            summary = registry.summary()
            rows = {str(item["key"]): item for item in summary["entries"]}
            self.assertEqual(rows["provider.kind"]["value"], "openai_codex")
            self.assertEqual(rows["provider.codex_command"]["value"], "codex")
            self.assertEqual(rows["provider.request_timeout"]["value"], "1800s")
            self.assertEqual(rows["provider.stale_timeout"]["value"], "90s")
            self.assertEqual(rows["channel.telegram_channel"]["state"], "needs-auth")
            self.assertEqual(rows["run.capability_trust_mode"]["value"], "trusted")
            self.assertEqual(rows["workspace.command_execution"]["state"], "trusted")
            self.assertEqual(rows["channel.telegram_channel"]["setup_hint"], "noruct channel telegram-configure")
            self.assertNotIn("TELEGRAM_TEST_TOKEN=", str(summary))
            result = registry.apply_global_change(
                {"key": "run.external_state_mode", "value": "blocked"}
            )
            self.assertEqual(result["value"], "blocked")
            registry.apply_global_change({"key": "run.capability_trust_mode", "value": "strict"})
            registry.apply_global_change({"key": "run.external_read_mode", "value": "blocked"})
            registry.apply_global_change({"key": "run.agent_settings_mode", "value": "blocked"})
            stored = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["run"]["external_state_mode"], "blocked")
            self.assertEqual(stored["run"]["capability_trust_mode"], "strict")
            self.assertEqual(stored["run"]["external_read_mode"], "blocked")
            self.assertEqual(stored["run"]["agent_settings_mode"], "blocked")
            _, apply = registry.tool_definitions()
            with self.assertRaises(Exception):
                apply.validator({"key": "run.external_state_mode", "value": "ask", "secret": "no"})

    def test_registry_rejects_agent_secret_and_unknown_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            write_global_runtime_settings(
                path,
                GlobalRuntimeSettings.from_mapping(
                    {"provider": {"kind": "openai_codex", "codex_command": "codex"}}
                ),
            )
            registry = SettingsRegistry(path)
            with self.assertRaises(ValueError):
                registry.apply_global_change({"key": "telegram.token_env", "value": "SECRET"})

    def test_external_read_ask_and_disabling_a_capability_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                "[provider]\nkind = \"openai_codex\"\ncodex_command = \"codex\"\n\n"
                "[run]\nstate = \"runtime.db\"\npermission_mode = \"ask\"\n\n"
                "[web_search]\nbase_url = \"http://127.0.0.1:8080\"\n\n"
                "[telegram_channel]\nenabled = true\ntoken_env = \"TELEGRAM_TEST_TOKEN\"\n",
                encoding="utf-8",
            )
            registry = SettingsRegistry(path)
            registry.apply_global_change({"key": "run.external_read_mode", "value": "ask"})
            disabled = registry.disable_configured_entry("integration.web_search")
            stored = tomllib.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(stored["run"]["external_read_mode"], "ask")
        self.assertNotIn("web_search", stored)
        self.assertIn("telegram_channel", stored)
        self.assertEqual(disabled["disabled"], "integration.web_search")

    def test_registry_marks_configured_capability_withheld_by_global_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                "[provider]\nkind = \"openai_codex\"\ncodex_command = \"codex\"\n\n"
                "[run]\nstate = \"runtime.db\"\nexternal_read_mode = \"blocked\"\n"
                "external_state_mode = \"blocked\"\n\n"
                "[web_search]\nbase_url = \"http://127.0.0.1:8080\"\n\n"
                "[computer_use]\ndriver_command = \"driver\"\nallowed_apps = [\"Terminal\"]\n",
                encoding="utf-8",
            )
            rows = {entry.key: entry for entry in SettingsRegistry(path).entries()}

        self.assertEqual(rows["integration.web_search"].state, "withheld")
        self.assertIn("External reads", rows["integration.web_search"].summary)
        self.assertEqual(rows["integration.computer_use"].state, "withheld")
        self.assertIn("External state changes", rows["integration.computer_use"].summary)
