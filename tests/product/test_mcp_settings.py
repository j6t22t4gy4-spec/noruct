from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from dynamic_firm.mcp_connector import (
    McpActionConfig,
    McpActionConfigSet,
    McpReadOnlyConfig,
    McpReadOnlyConfigSet,
    config_from_settings,
)
from dynamic_firm.product.mcp_action_settings import (
    append_mcp_action_settings,
    configured_mcp_action_policy,
    remove_mcp_action_profile_settings,
    remove_mcp_action_settings,
    write_mcp_action_settings,
)
from dynamic_firm.product.mcp_settings import (
    append_mcp_settings,
    extract_mcp_table,
    remove_mcp_profile_settings,
    remove_mcp_settings,
    write_mcp_settings,
)
from dynamic_firm.product.setup import SetupConfig, write_setup_config


class McpSettingsTests(unittest.TestCase):
    def _config(self) -> McpReadOnlyConfig:
        executable = Path(sys.executable).resolve()
        return McpReadOnlyConfig(
            python_command=executable,
            server_command=executable,
            server_args=("-m", "fixture"),
            tool_names=("read_issue", "read_note"),
            environment_names=("MCP_TOKEN",),
        )

    def test_atomic_replace_preserves_provider_and_run_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            target.write_text("[provider]\nmodel = \"one\"\n\n[run]\nmax_tool_calls = 8\n", encoding="utf-8")
            write_mcp_settings(target, self._config())
            content = target.read_text(encoding="utf-8")
            self.assertIn('[provider]\nmodel = "one"', content)
            self.assertIn("[mcp]", content)
            parsed = config_from_settings({
                "mcp": {
                    "enabled": True,
                    "python_command": str(Path(sys.executable).resolve()),
                    "server_command": str(Path(sys.executable).resolve()),
                    "server_args": ["-m", "fixture"],
                    "tool_names": ["read_issue", "read_note"],
                    "profile": "external-context",
                    "environment": ["MCP_TOKEN"],
                    "timeout_seconds": 10,
                    "max_result_bytes": 48000,
                }
            })
            assert parsed is not None
            self.assertEqual(len(parsed.selected_runtime_tool_names()), 2)
            self.assertEqual(target.stat().st_mode & 0o077, 0)

    def test_setup_retains_existing_mcp_table_and_disable_removes_only_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            write_mcp_settings(target, self._config())
            write_setup_config(
                target,
                SetupConfig(base_url="http://localhost:11434/v1", model="local"),
                overwrite=True,
            )
            self.assertIsNotNone(extract_mcp_table(target.read_text(encoding="utf-8")))
            self.assertTrue(remove_mcp_settings(target))
            content = target.read_text(encoding="utf-8")
            self.assertNotIn("[mcp]", content)
            self.assertIn("[provider]", content)
            self.assertFalse(remove_mcp_settings(target))

    def test_streamable_http_profile_round_trips_without_a_server_command_or_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            config = McpReadOnlyConfig(
                python_command=Path(sys.executable).resolve(),
                tool_name="read_issue",
                transport="streamable_http",
                server_url="https://mcp.example.invalid/v1",
                header_environment=(("Authorization", "MCP_HTTP_TOKEN"),),
            )
            write_mcp_settings(target, config)
            text = target.read_text(encoding="utf-8")
            parsed = config_from_settings(tomllib.loads(text))

        self.assertNotIn("fixture-http-token", text)
        self.assertNotIn("server_command", text)
        self.assertIn('server_url = "https://mcp.example.invalid/v1"', text)
        self.assertIsInstance(parsed, McpReadOnlyConfig)
        assert isinstance(parsed, McpReadOnlyConfig)
        self.assertEqual(parsed.transport, "streamable_http")
        self.assertEqual(parsed.header_environment, (("Authorization", "MCP_HTTP_TOKEN"),))

    def test_oauth_profile_round_trips_only_environment_names_and_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            config = McpReadOnlyConfig(
                python_command=Path(sys.executable).resolve(),
                tool_name="read_issue",
                transport="streamable_http",
                server_url="https://mcp.example.invalid/v1",
                oauth_enabled=True,
                oauth_client_id_environment="MCP_CLIENT_ID",
                oauth_client_secret_environment="MCP_CLIENT_SECRET",
                oauth_scope="context.read profile",
            )
            write_mcp_settings(target, config)
            text = target.read_text(encoding="utf-8")
            parsed = config_from_settings(tomllib.loads(text))

        self.assertIn('oauth_client_id_environment = "MCP_CLIENT_ID"', text)
        self.assertIn('oauth_client_secret_environment = "MCP_CLIENT_SECRET"', text)
        self.assertNotIn("fixture-client-secret", text)
        assert isinstance(parsed, McpReadOnlyConfig)
        self.assertTrue(parsed.oauth_enabled)
        self.assertEqual(parsed.oauth_scope, "context.read profile")

    def test_profiles_are_appended_and_removed_as_bounded_local_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            first = self._config()
            second = McpReadOnlyConfig(
                python_command=Path(sys.executable).resolve(),
                server_command=Path(sys.executable).resolve(),
                server_args=("-m", "another_fixture"),
                tool_name="read_note",
                profile="issue-context",
                environment_names=("ISSUE_TOKEN",),
            )
            write_mcp_settings(target, first)
            append_mcp_settings(target, second)
            parsed = config_from_settings(tomllib.loads(target.read_text(encoding="utf-8")))
            self.assertIsInstance(parsed, McpReadOnlyConfigSet)
            assert isinstance(parsed, McpReadOnlyConfigSet)
            self.assertEqual(tuple(item.profile for item in parsed.configs), ("external-context", "issue-context"))
            self.assertTrue(remove_mcp_profile_settings(target, "external-context"))
            parsed_after = config_from_settings(tomllib.loads(target.read_text(encoding="utf-8")))
            self.assertIsInstance(parsed_after, McpReadOnlyConfig)
            assert isinstance(parsed_after, McpReadOnlyConfig)
            self.assertEqual(parsed_after.profile, "issue-context")
            self.assertFalse(remove_mcp_profile_settings(target, "not-configured"))

    def test_action_profile_round_trips_separately_from_read_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            write_mcp_settings(target, self._config())
            action = McpActionConfig(
                python_command=Path(sys.executable).resolve(),
                server_command=Path(sys.executable).resolve(),
                server_args=("-m", "fixture"),
                tool_name="write_issue",
                profile="issue-action",
                environment_names=("ISSUE_ACTION_TOKEN",),
            )
            write_mcp_action_settings(target, action)
            restored = configured_mcp_action_policy(target)

            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.profile, "issue-action")
            self.assertIn("[mcp]", target.read_text(encoding="utf-8"))
            self.assertTrue(remove_mcp_action_settings(target))
            self.assertIn("[mcp]", target.read_text(encoding="utf-8"))
            self.assertNotIn("[mcp_action]", target.read_text(encoding="utf-8"))

    def test_https_action_profile_round_trips_without_credentials_or_server_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            action = McpActionConfig(
                python_command=Path(sys.executable).resolve(),
                tool_name="write_ticket",
                profile="remote-ticket-action",
                transport="streamable_http",
                server_url="https://mcp-action.example.invalid/v1",
                header_environment=(("Authorization", "MCP_ACTION_TOKEN"),),
                oauth_enabled=True,
                oauth_client_id_environment="MCP_CLIENT_ID",
                oauth_scope="tickets.write",
            )
            write_mcp_action_settings(target, action)
            text = target.read_text(encoding="utf-8")
            restored = configured_mcp_action_policy(target)

        self.assertNotIn("fixture-action-token", text)
        self.assertNotIn("server_command", text)
        self.assertIn('server_url = "https://mcp-action.example.invalid/v1"', text)
        assert isinstance(restored, McpActionConfig)
        self.assertEqual(restored.transport, "streamable_http")
        self.assertEqual(restored.header_environment, (("Authorization", "MCP_ACTION_TOKEN"),))
        self.assertTrue(restored.oauth_enabled)

    def test_action_profiles_can_be_added_then_individually_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            first = McpActionConfig(
                python_command=Path(sys.executable).resolve(), server_command=Path(sys.executable).resolve(),
                tool_name="write_issue", profile="issues",
            )
            second = McpActionConfig(
                python_command=Path(sys.executable).resolve(), server_command=Path(sys.executable).resolve(),
                tool_name="send_notice", profile="notices",
            )
            write_mcp_action_settings(target, first)
            append_mcp_action_settings(target, second)
            restored = configured_mcp_action_policy(target)
            self.assertIsInstance(restored, McpActionConfigSet)
            assert isinstance(restored, McpActionConfigSet)
            self.assertEqual([item.profile for item in restored.configs], ["issues", "notices"])
            self.assertTrue(remove_mcp_action_profile_settings(target, "issues"))
            self.assertEqual(configured_mcp_action_policy(target), second)
