from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, RunCommandConfig, _action_policy, main
from dynamic_firm.home_assistant import (
    HomeAssistantConfig,
    HomeAssistantTools,
    config_from_settings,
    write_home_assistant_settings,
)
from dynamic_firm.runtime.models import RunLimits, ToolEffect
from dynamic_firm.runtime.ports import CancellationToken


class _Response:
    def __init__(self, body: object) -> None: self.body = json.dumps(body).encode()
    def __enter__(self): return self
    def __exit__(self, *_): return None
    def read(self, _): return self.body


class HomeAssistantToolTests(unittest.TestCase):
    def test_allowlisted_read_and_approved_service_definition(self) -> None:
        old = os.environ.get("HASS_TEST_TOKEN"); os.environ["HASS_TEST_TOKEN"] = "secret"
        try:
            config = HomeAssistantConfig("http://127.0.0.1:8123", "HASS_TEST_TOKEN", ("light.kitchen", "sensor.temp_*"), ("light.turn_on",))
            tools = {item.name: item for item in HomeAssistantTools(config).definitions()}
            self.assertEqual(tools["get_home_assistant_state"].effect, ToolEffect.NETWORK)
            self.assertEqual(tools["call_home_assistant_service"].effect, ToolEffect.EXECUTE)
            self.assertTrue(tools["call_home_assistant_service"].requires_approval)
            with patch("dynamic_firm.home_assistant.urlopen", return_value=_Response({"entity_id": "light.kitchen", "state": "off", "attributes": {"friendly_name": "Kitchen"}})) as opener:
                arguments = tools["get_home_assistant_state"].validator({"entity_id": "light.kitchen"})
                result = json.loads(asyncio.run(tools["get_home_assistant_state"].handler(arguments, CancellationToken())))
            self.assertEqual(result["state"], "off")
            self.assertIn("/api/states/light.kitchen", opener.call_args.args[0].full_url)
            with self.assertRaises(Exception):
                asyncio.run(tools["get_home_assistant_state"].handler({"entity_id": "switch.unlisted"}, CancellationToken()))
            with self.assertRaises(Exception):
                asyncio.run(tools["call_home_assistant_service"].handler({"domain": "switch", "service": "turn_on", "entity_id": "light.kitchen"}, CancellationToken()))
        finally:
            if old is None: os.environ.pop("HASS_TEST_TOKEN", None)
            else: os.environ["HASS_TEST_TOKEN"] = old

    def test_settings_and_cli_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            config = HomeAssistantConfig("http://127.0.0.1:8123", allowed_entities=("light.kitchen",), allowed_services=("light.turn_on",))
            write_home_assistant_settings(path, config)
            path.write_text('[provider]\nbase_url = "http://127.0.0.1:1"\nmodel = "fixture"\nno_auth = true\n\n' + path.read_text(encoding="utf-8"), encoding="utf-8")
            self.assertEqual(config_from_settings(__import__("tomllib").loads(path.read_text(encoding="utf-8"))), config)
            output = io.StringIO()
            self.assertEqual(main(["--config", str(path), "home-assistant", "status", "--json"], stdout=output, stderr=io.StringIO()), EXIT_INPUT)
            status = json.loads(output.getvalue()); self.assertTrue(status["enabled"]); self.assertFalse(status["ready"])
            configured = io.StringIO()
            self.assertEqual(main(["--config", str(path), "home-assistant", "configure", "--base-url", "http://127.0.0.1:8123", "--allow-entity", "light.kitchen", "--allow-service", "light.turn_on", "--json"], stdout=configured, stderr=io.StringIO()), EXIT_OK)
            self.assertTrue(json.loads(configured.getvalue())["configuration_changed"])

    def test_company_action_policy_binds_reads_and_approved_service_call(self) -> None:
        config = RunCommandConfig(
            goal="inspect", workspace=Path.cwd(), state_path=Path("/tmp/home-assistant-test.db"),
            provider_kind="openai_api", base_url="http://127.0.0.1:1", model="fixture",
            codex_model=None, codex_command="codex", api_key_env=None, request_timeout_seconds=1,
            permission_mode="ask", run_limits=RunLimits(1_000, 1, 8, 10_000, 1.0),
            home_assistant=HomeAssistantConfig("http://127.0.0.1:8123", allowed_entities=("light.kitchen",), allowed_services=("light.turn_on",)),
        )
        grants = {grant.tool_name: grant for grant in _action_policy(config).tool_grants}
        self.assertEqual(grants["get_home_assistant_state"].allowed_effects, (ToolEffect.NETWORK,))
        self.assertEqual(grants["call_home_assistant_service"].allowed_effects, (ToolEffect.EXECUTE,))
        self.assertTrue(grants["call_home_assistant_service"].requires_approval)
        self.assertNotIn("call_home_assistant_service", _action_policy(config).auto_approved_tool_names)
        automatic = _action_policy(replace(config, external_state_mode="user-authorized-auto"))
        self.assertIn("call_home_assistant_service", automatic.auto_approved_tool_names)

    def test_read_capability_remains_available_when_external_actions_are_blocked(self) -> None:
        config = RunCommandConfig(
            goal="inspect", workspace=Path.cwd(), state_path=Path("/tmp/home-assistant-test.db"),
            provider_kind="openai_api", base_url="http://127.0.0.1:1", model="fixture",
            codex_model=None, codex_command="codex", api_key_env=None, request_timeout_seconds=1,
            permission_mode="read-only", external_state_mode="blocked", external_read_mode="allow",
            run_limits=RunLimits(1_000, 1, 8, 10_000, 1.0),
            home_assistant=HomeAssistantConfig("http://127.0.0.1:8123", allowed_entities=("light.kitchen",), allowed_services=("light.turn_on",)),
        )
        names = {grant.tool_name for grant in _action_policy(config).tool_grants}
        self.assertIn("get_home_assistant_state", names)
        self.assertNotIn("call_home_assistant_service", names)
