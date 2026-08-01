from __future__ import annotations

import unittest
import io
import json
import tempfile
from pathlib import Path

from dynamic_firm.cli import EXIT_OK, _provider_config, _run_config, build_parser, main
from dynamic_firm.product.setup import SetupConfig
from dynamic_firm.providers.anthropic import AnthropicProviderConfig
from dynamic_firm.providers.openai_compat import OpenAICompatProviderConfig
from dynamic_firm.providers.vertex import VertexProviderConfig
from dynamic_firm.providers.bedrock import BedrockProviderConfig
from dynamic_firm.providers.moa import MoAProviderConfig


class ProviderProfileTests(unittest.TestCase):
    def config(self, provider: str):
        args = build_parser().parse_args(
            ["ask", "hello", "--provider", provider, "--model", "contract-model"]
        )
        return _run_config(args, {})

    def test_anthropic_profile_uses_native_transport_and_environment_secret(self) -> None:
        run = self.config("anthropic-api")
        provider = _provider_config(run)
        self.assertIsInstance(provider, AnthropicProviderConfig)
        self.assertEqual(provider.base_url, "https://api.anthropic.com/v1")
        self.assertEqual(provider.api_key_env, "ANTHROPIC_API_KEY")

    def test_gemini_profile_reuses_official_openai_compatible_transport(self) -> None:
        run = self.config("gemini-api")
        provider = _provider_config(run)
        self.assertIsInstance(provider, OpenAICompatProviderConfig)
        self.assertEqual(
            provider.base_url,
            "https://generativelanguage.googleapis.com/v1beta/openai",
        )
        self.assertEqual(provider.api_key_env, "GEMINI_API_KEY")

    def test_ollama_profile_defaults_to_loopback_without_credential(self) -> None:
        run = self.config("ollama")
        provider = _provider_config(run)
        self.assertIsInstance(provider, OpenAICompatProviderConfig)
        self.assertEqual(provider.base_url, "http://localhost:11434/v1")
        self.assertIsNone(provider.api_key_env)

    def test_cli_and_configured_moa_reference_routes_wrap_the_active_aggregator(self) -> None:
        args = build_parser().parse_args(["ask", "hello", "--provider", "ollama", "--model", "aggregator", "--moa-reference", "gemini:advisor"])
        configured = _provider_config(_run_config(args, {}))
        self.assertIsInstance(configured, MoAProviderConfig)
        self.assertEqual(configured.references[0][0], "gemini_api:advisor")
        configured_from_file = _provider_config(_run_config(build_parser().parse_args(["ask", "hello", "--provider", "ollama", "--model", "aggregator"]), {"provider": {"moa_references": [{"kind": "gemini", "model": "advisor"}]}}))
        self.assertIsInstance(configured_from_file, MoAProviderConfig)

    def test_capabilities_reports_configured_moa_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('[provider]\nmoa_references = [{ kind = "gemini", model = "advisor" }]\n', encoding="utf-8")
            output = io.StringIO()
            code = main(["--config", str(config), "capabilities", "status", "--json"], stdout=output, stderr=io.StringIO())
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(json.loads(output.getvalue())["mixture_of_agents"]["enabled"])

    def test_vertex_uses_a_first_class_user_managed_adc_transport(self) -> None:
        args = build_parser().parse_args([
            "ask", "hello", "--provider", "vertex", "--model", "google/gemini-2.5-flash",
            "--base-url", "https://us-central1-aiplatform.googleapis.com/v1/projects/project_1/locations/us-central1/endpoints/openapi",
        ])
        configured = _provider_config(_run_config(args, {}))
        self.assertIsInstance(configured, VertexProviderConfig)
        self.assertEqual(configured.model, "google/gemini-2.5-flash")

    def test_bedrock_uses_the_native_converse_transport_and_bearer_token_name(self) -> None:
        configured = _provider_config(self.config("bedrock"))
        self.assertIsInstance(configured, BedrockProviderConfig)
        self.assertEqual(configured.base_url, "https://bedrock-runtime.us-east-1.amazonaws.com")
        self.assertEqual(configured.api_key_env, "AWS_BEARER_TOKEN_BEDROCK")

    def test_openai_compatible_provider_registry_reuses_the_bounded_parent_transport(self) -> None:
        remote = _provider_config(self.config("openrouter"))
        local = _provider_config(self.config("lmstudio"))
        self.assertIsInstance(remote, OpenAICompatProviderConfig)
        self.assertEqual(remote.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(remote.api_key_env, "OPENROUTER_API_KEY")
        self.assertIsInstance(local, OpenAICompatProviderConfig)
        self.assertEqual(local.base_url, "http://localhost:1234/v1")
        self.assertIsNone(local.api_key_env)
        for provider, endpoint, credential in (
            ("deepseek", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
            ("fireworks", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY"),
            ("xai", "https://api.x.ai/v1", "XAI_API_KEY"),
            ("deepinfra", "https://api.deepinfra.com/v1/openai", "DEEPINFRA_TOKEN"),
            ("nvidia", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
            ("alibaba", "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
            ("zai", "https://api.z.ai/api/paas/v4", "ZAI_API_KEY"),
            ("arcee", "https://api.arcee.ai/api/v1", "ARCEEAI_API_KEY"),
            ("gmi", "https://api.gmi-serving.com/v1", "GMI_API_KEY"),
            ("stepfun", "https://api.stepfun.com/v1", "STEPFUN_API_KEY"),
            ("kilo", "https://api.kilo.ai/api/gateway", "KILO_API_KEY"),
            ("kimi", "https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"),
            ("novita", "https://api.novita.ai/openai/v1", "NOVITA_API_KEY"),
            ("xiaomi", "https://api.xiaomimimo.com/v1", "MIMO_API_KEY"),
            ("opencode-zen", "https://opencode.ai/zen/v1", "OPENCODE_ZEN_API_KEY"),
            ("ollama-cloud", "https://ollama.com/v1", "OLLAMA_API_KEY"),
            ("alibaba-coding", "https://coding-intl.dashscope.aliyuncs.com/v1", "ALIBABA_CODING_PLAN_API_KEY"),
            ("nous", "https://inference.nousresearch.com/v1", "NOUS_API_KEY"),
        ):
            with self.subTest(provider=provider):
                configured = _provider_config(self.config(provider))
                self.assertIsInstance(configured, OpenAICompatProviderConfig)
                self.assertEqual(configured.base_url, endpoint)
                self.assertEqual(configured.api_key_env, credential)

    def test_azure_foundry_requires_a_resource_endpoint_and_uses_documented_api_key_header(self) -> None:
        args = build_parser().parse_args(
            [
                "ask", "hello", "--provider", "azure-foundry",
                "--base-url", "https://example.openai.azure.com/openai/v1",
                "--model", "deployment-name",
            ]
        )
        configured = _provider_config(_run_config(args, {}))
        self.assertIsInstance(configured, OpenAICompatProviderConfig)
        self.assertEqual(configured.api_key_env, "AZURE_FOUNDRY_API_KEY")
        self.assertEqual(configured.credential_header, "api-key")
        self.assertEqual(configured.credential_prefix, "")

    def test_huggingface_profile_reuses_the_bounded_parent_transport(self) -> None:
        configured = _provider_config(self.config("huggingface"))
        self.assertIsInstance(configured, OpenAICompatProviderConfig)
        self.assertEqual(configured.base_url, "https://router.huggingface.co/v1")
        self.assertEqual(configured.api_key_env, "HF_TOKEN")

    def test_minimax_profile_reuses_the_native_anthropic_transport(self) -> None:
        configured = _provider_config(self.config("minimax"))
        self.assertIsInstance(configured, AnthropicProviderConfig)
        self.assertEqual(configured.base_url, "https://api.minimax.io/anthropic/v1")
        self.assertEqual(configured.api_key_env, "MINIMAX_API_KEY")

    def test_anthropic_compatible_profiles_do_not_allow_anonymous_transport(self) -> None:
        args = build_parser().parse_args(
            ["ask", "hello", "--provider", "minimax", "--model", "MiniMax-M2.7", "--no-auth"]
        )
        with self.assertRaisesRegex(ValueError, "MiniMax requires an environment-backed credential"):
            _run_config(args, {})

    def test_setup_renders_each_provider_without_secret_values(self) -> None:
        cases = (
            SetupConfig(
                provider_kind="anthropic_api",
                base_url="https://api.anthropic.com/v1",
                model="claude-contract",
                api_key_env="ANTHROPIC_API_KEY",
            ),
            SetupConfig(
                provider_kind="gemini_api",
                base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                model="gemini-contract",
                api_key_env="GEMINI_API_KEY",
            ),
            SetupConfig(
                provider_kind="ollama",
                base_url="http://localhost:11434/v1",
                model="qwen-contract",
                api_key_env="",
                no_auth=True,
            ),
            SetupConfig(
                provider_kind="openrouter",
                base_url="https://openrouter.ai/api/v1",
                model="openai/gpt-5.2",
                api_key_env="OPENROUTER_API_KEY",
            ),
            SetupConfig(
                provider_kind="lmstudio",
                base_url="http://localhost:1234/v1",
                model="local-model",
                api_key_env="",
                no_auth=True,
            ),
            SetupConfig(
                provider_kind="deepseek",
                base_url="https://api.deepseek.com",
                model="deepseek-v4-pro",
                api_key_env="DEEPSEEK_API_KEY",
            ),
            SetupConfig(
                provider_kind="fireworks",
                base_url="https://api.fireworks.ai/inference/v1",
                model="accounts/fireworks/models/contract",
                api_key_env="FIREWORKS_API_KEY",
            ),
            SetupConfig(
                provider_kind="xai",
                base_url="https://api.x.ai/v1",
                model="grok-4.5",
                api_key_env="XAI_API_KEY",
            ),
            SetupConfig(
                provider_kind="huggingface",
                base_url="https://router.huggingface.co/v1",
                model="openai/gpt-oss-120b:fastest",
                api_key_env="HF_TOKEN",
            ),
            SetupConfig(
                provider_kind="minimax",
                base_url="https://api.minimax.io/anthropic/v1",
                model="MiniMax-M2.7",
                api_key_env="MINIMAX_API_KEY",
            ),
            SetupConfig(
                provider_kind="deepinfra",
                base_url="https://api.deepinfra.com/v1/openai",
                model="deepseek-ai/DeepSeek-V3",
                api_key_env="DEEPINFRA_TOKEN",
            ),
            SetupConfig(
                provider_kind="nvidia",
                base_url="https://integrate.api.nvidia.com/v1",
                model="openai/gpt-oss-120b",
                api_key_env="NVIDIA_API_KEY",
            ),
            SetupConfig(
                provider_kind="alibaba",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                model="qwen-plus",
                api_key_env="DASHSCOPE_API_KEY",
            ),
            SetupConfig(
                provider_kind="zai",
                base_url="https://api.z.ai/api/paas/v4",
                model="glm-4.7",
                api_key_env="ZAI_API_KEY",
            ),
            SetupConfig(
                provider_kind="arcee",
                base_url="https://api.arcee.ai/api/v1",
                model="trinity-mini",
                api_key_env="ARCEEAI_API_KEY",
            ),
            SetupConfig(
                provider_kind="gmi",
                base_url="https://api.gmi-serving.com/v1",
                model="deepseek-ai/DeepSeek-R1",
                api_key_env="GMI_API_KEY",
            ),
            SetupConfig(
                provider_kind="stepfun",
                base_url="https://api.stepfun.com/v1",
                model="step-3.5-flash",
                api_key_env="STEPFUN_API_KEY",
            ),
            SetupConfig(
                provider_kind="kilo",
                base_url="https://api.kilo.ai/api/gateway",
                model="anthropic/claude-sonnet-4.5",
                api_key_env="KILO_API_KEY",
            ),
            SetupConfig(
                provider_kind="kimi",
                base_url="https://api.moonshot.ai/v1",
                model="kimi-k2.6",
                api_key_env="MOONSHOT_API_KEY",
            ),
            SetupConfig(
                provider_kind="novita",
                base_url="https://api.novita.ai/openai/v1",
                model="deepseek/deepseek-r1",
                api_key_env="NOVITA_API_KEY",
            ),
            SetupConfig(
                provider_kind="xiaomi",
                base_url="https://api.xiaomimimo.com/v1",
                model="mimo-v2.5-pro",
                api_key_env="MIMO_API_KEY",
            ),
        )
        for config in cases:
            with self.subTest(provider=config.provider_kind):
                rendered = config.render()
                self.assertIn(f'kind = "{config.provider_kind}"', rendered)
                self.assertNotIn("api_key_value", rendered)


if __name__ == "__main__":
    unittest.main()
