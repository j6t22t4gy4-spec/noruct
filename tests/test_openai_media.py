from __future__ import annotations

import asyncio
import base64
import io
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, RunCommandConfig, _action_policy, main
from dynamic_firm.openai_media import OpenAIMediaConfig, OpenAIMediaConnector, media_config_from_settings
from dynamic_firm.runtime.models import RunLimits, ToolEffect
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.tools import ToolValidationError


class OpenAIMediaConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)
        self.config = OpenAIMediaConfig(
            image_enabled=True,
            speech_enabled=True,
            transcription_enabled=True,
            video_enabled=True,
        )
        self.connector = OpenAIMediaConnector(self.config, self.workspace, workspace_id="workspace")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_selected_tools_are_high_risk_network_actions(self) -> None:
        tools = {item.name: item for item in self.connector.definitions()}
        self.assertEqual(set(tools), {"generate_image", "synthesize_speech", "transcribe_audio", "generate_video"})
        for item in tools.values():
            self.assertTrue(item.requires_approval)
            self.assertFalse(item.allow_session_approval)
            self.assertEqual(item.effect.value, "NETWORK")

    def test_image_writes_only_a_new_workspace_artifact(self) -> None:
        definition = next(item for item in self.connector.definitions() if item.name == "generate_image")
        self.connector._json_request = lambda path, payload: {"data": [{"b64_json": base64.b64encode(b"png").decode()}]}  # type: ignore[method-assign]
        arguments = definition.validator({"prompt": "a small test", "output_path": "artifacts/test.png"})
        result = asyncio.run(definition.handler(arguments, CancellationToken()))
        self.assertIn('"artifact": "artifacts/test.png"', result)
        self.assertEqual((self.workspace / "artifacts/test.png").read_bytes(), b"png")
        with self.assertRaises(ToolValidationError):
            definition.validator({"prompt": "again", "output_path": "artifacts/test.png"})
        with self.assertRaises(ToolValidationError):
            definition.validator({"prompt": "bad", "output_path": "../outside.png"})

    def test_speech_and_transcription_keep_paths_bounded(self) -> None:
        tools = {item.name: item for item in self.connector.definitions()}
        self.connector._bytes_request = lambda path, payload: b"audio"  # type: ignore[method-assign]
        speech = tools["synthesize_speech"]
        arguments = speech.validator({"text": "hello", "output_path": "artifacts/voice.mp3"})
        asyncio.run(speech.handler(arguments, CancellationToken()))
        self.assertEqual((self.workspace / "artifacts/voice.mp3").read_bytes(), b"audio")
        source = self.workspace / "artifacts/input.wav"; source.write_bytes(b"audio")
        self.connector._multipart_request = lambda path, fields, source: {"text": "heard text"}  # type: ignore[method-assign]
        transcript = tools["transcribe_audio"]
        result = asyncio.run(transcript.handler(transcript.validator({"input_path": "artifacts/input.wav"}), CancellationToken()))
        self.assertIn("heard text", result)
        with self.assertRaises(ToolValidationError):
            transcript.validator({"input_path": "../input.wav"})

    def test_configuration_rejects_empty_capability_set(self) -> None:
        with self.assertRaises(ValueError):
            media_config_from_settings({"openai_media": {"enabled": True}})
        config = media_config_from_settings({"openai_media": {"enabled": True, "image_enabled": True}})
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.enabled_capabilities, ("image",))

    def test_cli_configure_and_status_never_store_the_credential_value(self) -> None:
        config_path = self.workspace / "config.toml"
        output = io.StringIO()
        error = io.StringIO()
        result = main(
            ["--config", str(config_path), "media", "configure", "--enable", "image", "--enable", "transcription", "--json"],
            stdout=output,
            stderr=error,
        )
        self.assertEqual(result, EXIT_INPUT)
        config_text = config_path.read_text(encoding="utf-8")
        self.assertIn("[openai_media]", config_text)
        self.assertNotIn("sk-", config_text)
        status = main(["--config", str(config_path), "media", "status", "--json"], stdout=output, stderr=error)
        self.assertEqual(status, EXIT_INPUT)

    def test_company_policy_grants_only_enabled_media_operations(self) -> None:
        config = RunCommandConfig(
            goal="make media", workspace=self.workspace, state_path=self.workspace / "runtime.db",
            provider_kind="openai_api", base_url="https://api.openai.com/v1", model="gpt-4.1-mini",
            codex_model=None, codex_command="codex", api_key_env="OPENAI_API_KEY",
            request_timeout_seconds=30.0, permission_mode="ask", run_limits=RunLimits(),
            openai_media=OpenAIMediaConfig(image_enabled=True, transcription_enabled=True),
        )
        grants = {item.tool_name: item for item in _action_policy(config).tool_grants}
        self.assertEqual(set(grants).intersection({"generate_image", "synthesize_speech", "transcribe_audio", "generate_video"}), {"generate_image", "transcribe_audio"})
        self.assertEqual(grants["generate_image"].allowed_effects, (ToolEffect.NETWORK,))
        self.assertTrue(grants["generate_image"].requires_approval)
        self.assertEqual(_action_policy(config).network_policy, "EXTERNAL_READ_ONLY")

    def test_read_only_workspace_with_configured_media_keeps_a_valid_no_network_policy(self) -> None:
        config = RunCommandConfig(
            goal="answer normally", workspace=self.workspace, state_path=self.workspace / "runtime.db",
            provider_kind="openai_api", base_url="https://api.openai.com/v1", model="gpt-4.1-mini",
            codex_model=None, codex_command="codex", api_key_env="OPENAI_API_KEY",
            request_timeout_seconds=30.0, permission_mode="read-only", run_limits=RunLimits(),
            openai_media=OpenAIMediaConfig(image_enabled=True),
        )

        policy = _action_policy(config)

        self.assertNotIn("generate_image", {item.tool_name for item in policy.tool_grants})
        self.assertEqual(policy.network_policy, "DENY")
