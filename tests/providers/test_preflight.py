from __future__ import annotations

import json
import os
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dynamic_firm.providers.preflight import (
    ProviderPreflightConfig,
    probe_provider_metadata,
    provider_preflight_status,
)


class _MetadataHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.captures.append(
            {"path": self.path, "authorization": self.headers.get("Authorization")}
        )
        raw = json.dumps(self.server.payload).encode("utf-8")
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args) -> None:
        return


@contextmanager
def metadata_server(*, status: int = 200, payload: object | None = None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MetadataHandler)
    server.status = status
    server.payload = payload if payload is not None else {"data": [{"id": "contract-model"}]}
    server.captures = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


class ProviderPreflightTests(unittest.TestCase):
    def config(self, base_url: str, **changes) -> ProviderPreflightConfig:
        return ProviderPreflightConfig(
            kind="openai_api",
            base_url=base_url,
            model="contract-model",
            api_key_env="PREFLIGHT_TEST_KEY",
            no_auth=False,
            **changes,
        )

    def test_status_is_local_and_never_exposes_a_credential_value(self) -> None:
        prior = os.environ.pop("PREFLIGHT_TEST_KEY", None)
        try:
            result = provider_preflight_status(self.config("http://127.0.0.1:9999/v1"))
        finally:
            if prior is not None:
                os.environ["PREFLIGHT_TEST_KEY"] = prior
        self.assertEqual(result.outcome, "CREDENTIAL_MISSING")
        self.assertFalse(result.network_attempted)
        self.assertFalse(result.credential_value_exposed)

    def test_external_process_preflight_never_guesses_an_http_endpoint(self) -> None:
        result = provider_preflight_status(
            ProviderPreflightConfig(
                kind="external_exec", base_url="", model="subscription-model", api_key_env=None, no_auth=True
            )
        )
        self.assertEqual(result.outcome, "EXTERNAL_PROCESS_READINESS_CHECK_REQUIRED")
        self.assertFalse(result.network_attempted)
        self.assertIsNone(result.endpoint)

    def test_gmi_uses_the_documented_openai_models_endpoint(self) -> None:
        result = provider_preflight_status(
            ProviderPreflightConfig(
                kind="gmi", base_url="https://api.gmi-serving.com/v1", model="deepseek-ai/DeepSeek-R1", api_key_env="GMI_API_KEY", no_auth=False
            )
        )
        self.assertEqual(result.endpoint, "https://api.gmi-serving.com/v1/models")

    def test_profile_without_documented_model_list_does_not_guess_a_probe_url(self) -> None:
        previous = os.environ.get("MINIMAX_API_KEY")
        os.environ["MINIMAX_API_KEY"] = "fixture-only"
        try:
            result = provider_preflight_status(
                ProviderPreflightConfig(
                    kind="minimax",
                    base_url="https://api.minimax.io/anthropic/v1",
                    model="MiniMax-M2.7",
                    api_key_env="MINIMAX_API_KEY",
                    no_auth=False,
                )
            )
        finally:
            if previous is None:
                os.environ.pop("MINIMAX_API_KEY", None)
            else:
                os.environ["MINIMAX_API_KEY"] = previous
        self.assertEqual(result.outcome, "METADATA_PREFLIGHT_UNSUPPORTED")
        self.assertFalse(result.network_attempted)
        self.assertIsNone(result.endpoint)

    def test_azure_foundry_does_not_guess_a_resource_model_list_endpoint(self) -> None:
        previous = os.environ.get("AZURE_FOUNDRY_API_KEY")
        os.environ["AZURE_FOUNDRY_API_KEY"] = "fixture-only"
        try:
            result = provider_preflight_status(
                ProviderPreflightConfig(
                    kind="azure_foundry",
                    base_url="https://example.openai.azure.com/openai/v1",
                    model="deployment-name",
                    api_key_env="AZURE_FOUNDRY_API_KEY",
                    no_auth=False,
                )
            )
        finally:
            if previous is None:
                os.environ.pop("AZURE_FOUNDRY_API_KEY", None)
            else:
                os.environ["AZURE_FOUNDRY_API_KEY"] = previous
        self.assertEqual(result.outcome, "METADATA_PREFLIGHT_UNSUPPORTED")
        self.assertFalse(result.network_attempted)

    def test_deepinfra_uses_its_reviewed_sibling_model_catalogue_route(self) -> None:
        result = provider_preflight_status(
            ProviderPreflightConfig(
                kind="deepinfra",
                base_url="https://api.deepinfra.com/v1/openai",
                model="deepseek-ai/DeepSeek-V3",
                api_key_env="DEEPINFRA_TOKEN",
                no_auth=False,
            )
        )
        self.assertEqual(result.endpoint, "https://api.deepinfra.com/v1/models")

    def test_novita_uses_its_reviewed_sibling_model_catalogue_route(self) -> None:
        result = provider_preflight_status(
            ProviderPreflightConfig(
                kind="novita",
                base_url="https://api.novita.ai/openai/v1",
                model="deepseek/deepseek-r1",
                api_key_env="NOVITA_API_KEY",
                no_auth=False,
            )
        )
        self.assertEqual(result.endpoint, "https://api.novita.ai/v1/models")

    def test_xiaomi_does_not_guess_a_model_catalogue_route(self) -> None:
        previous = os.environ.get("MIMO_API_KEY")
        os.environ["MIMO_API_KEY"] = "fixture-only"
        try:
            result = provider_preflight_status(
                ProviderPreflightConfig(
                    kind="xiaomi",
                    base_url="https://api.xiaomimimo.com/v1",
                    model="mimo-v2.5-pro",
                    api_key_env="MIMO_API_KEY",
                    no_auth=False,
                )
            )
        finally:
            if previous is None:
                os.environ.pop("MIMO_API_KEY", None)
            else:
                os.environ["MIMO_API_KEY"] = previous
        self.assertEqual(result.outcome, "METADATA_PREFLIGHT_UNSUPPORTED")
        self.assertFalse(result.network_attempted)

    def test_nvidia_does_not_guess_a_model_catalogue_route(self) -> None:
        previous = os.environ.get("NVIDIA_API_KEY")
        os.environ["NVIDIA_API_KEY"] = "fixture-only"
        try:
            result = provider_preflight_status(
                ProviderPreflightConfig(
                    kind="nvidia",
                    base_url="https://integrate.api.nvidia.com/v1",
                    model="openai/gpt-oss-120b",
                    api_key_env="NVIDIA_API_KEY",
                    no_auth=False,
                )
            )
        finally:
            if previous is None:
                os.environ.pop("NVIDIA_API_KEY", None)
            else:
                os.environ["NVIDIA_API_KEY"] = previous
        self.assertEqual(result.outcome, "METADATA_PREFLIGHT_UNSUPPORTED")
        self.assertFalse(result.network_attempted)

    def test_alibaba_does_not_guess_a_model_catalogue_route(self) -> None:
        previous = os.environ.get("DASHSCOPE_API_KEY")
        os.environ["DASHSCOPE_API_KEY"] = "fixture-only"
        try:
            result = provider_preflight_status(
                ProviderPreflightConfig(
                    kind="alibaba",
                    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    model="qwen-plus",
                    api_key_env="DASHSCOPE_API_KEY",
                    no_auth=False,
                )
            )
        finally:
            if previous is None:
                os.environ.pop("DASHSCOPE_API_KEY", None)
            else:
                os.environ["DASHSCOPE_API_KEY"] = previous
        self.assertEqual(result.outcome, "METADATA_PREFLIGHT_UNSUPPORTED")
        self.assertFalse(result.network_attempted)

    def test_zai_does_not_guess_a_model_catalogue_route(self) -> None:
        previous = os.environ.get("ZAI_API_KEY")
        os.environ["ZAI_API_KEY"] = "fixture-only"
        try:
            result = provider_preflight_status(
                ProviderPreflightConfig(
                    kind="zai",
                    base_url="https://api.z.ai/api/paas/v4",
                    model="glm-4.7",
                    api_key_env="ZAI_API_KEY",
                    no_auth=False,
                )
            )
        finally:
            if previous is None:
                os.environ.pop("ZAI_API_KEY", None)
            else:
                os.environ["ZAI_API_KEY"] = previous
        self.assertEqual(result.outcome, "METADATA_PREFLIGHT_UNSUPPORTED")
        self.assertFalse(result.network_attempted)

    def test_arcee_does_not_guess_a_model_catalogue_route(self) -> None:
        previous = os.environ.get("ARCEEAI_API_KEY")
        os.environ["ARCEEAI_API_KEY"] = "fixture-only"
        try:
            result = provider_preflight_status(
                ProviderPreflightConfig(
                    kind="arcee",
                    base_url="https://api.arcee.ai/api/v1",
                    model="trinity-mini",
                    api_key_env="ARCEEAI_API_KEY",
                    no_auth=False,
                )
            )
        finally:
            if previous is None:
                os.environ.pop("ARCEEAI_API_KEY", None)
            else:
                os.environ["ARCEEAI_API_KEY"] = previous
        self.assertEqual(result.outcome, "METADATA_PREFLIGHT_UNSUPPORTED")
        self.assertFalse(result.network_attempted)

    def test_confirmed_metadata_probe_uses_get_models_and_only_reports_summary(self) -> None:
        previous = os.environ.get("PREFLIGHT_TEST_KEY")
        os.environ["PREFLIGHT_TEST_KEY"] = "must-not-appear"
        try:
            with metadata_server() as (server, base_url):
                result = probe_provider_metadata(self.config(base_url))
        finally:
            if previous is None:
                os.environ.pop("PREFLIGHT_TEST_KEY", None)
            else:
                os.environ["PREFLIGHT_TEST_KEY"] = previous
        self.assertEqual(result.outcome, "METADATA_REACHABLE")
        self.assertTrue(result.network_attempted)
        self.assertEqual(result.model_count, 1)
        self.assertTrue(result.configured_model_seen)
        self.assertEqual(server.captures[0]["path"], "/v1/models")
        self.assertEqual(server.captures[0]["authorization"], "Bearer must-not-appear")
        self.assertNotIn("must-not-appear", json.dumps(result.to_dict()))

    def test_auth_rejection_is_classified_without_response_body(self) -> None:
        previous = os.environ.get("PREFLIGHT_TEST_KEY")
        os.environ["PREFLIGHT_TEST_KEY"] = "fixture"
        try:
            with metadata_server(status=401, payload={"error": "private detail"}) as (_server, base_url):
                result = probe_provider_metadata(self.config(base_url))
        finally:
            if previous is None:
                os.environ.pop("PREFLIGHT_TEST_KEY", None)
            else:
                os.environ["PREFLIGHT_TEST_KEY"] = previous
        self.assertEqual(result.outcome, "AUTHENTICATION_REJECTED")
        self.assertEqual(result.http_status, 401)
        self.assertNotIn("private detail", result.details)

    def test_stepfun_does_not_guess_a_model_list_endpoint(self) -> None:
        previous = os.environ.get("STEPFUN_API_KEY")
        os.environ["STEPFUN_API_KEY"] = "fixture"
        try:
            result = provider_preflight_status(
                ProviderPreflightConfig(
                    kind="stepfun",
                    base_url="https://api.stepfun.com/v1",
                    model="step-3.5-flash",
                    api_key_env="STEPFUN_API_KEY",
                    no_auth=False,
                )
            )
        finally:
            if previous is None:
                os.environ.pop("STEPFUN_API_KEY", None)
            else:
                os.environ["STEPFUN_API_KEY"] = previous
        self.assertEqual(result.outcome, "METADATA_PREFLIGHT_UNSUPPORTED")
        self.assertFalse(result.network_attempted)

    def test_kilo_and_kimi_use_the_documented_models_endpoint(self) -> None:
        for kind, base_url, key_name in (
            ("kilo", "https://api.kilo.ai/api/gateway", "KILO_API_KEY"),
            ("kimi", "https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"),
        ):
            with self.subTest(kind=kind):
                previous = os.environ.get(key_name)
                os.environ[key_name] = "fixture"
                try:
                    config = ProviderPreflightConfig(
                        kind=kind,
                        base_url=base_url,
                        model="contract-model",
                        api_key_env=key_name,
                        no_auth=False,
                    )
                    endpoint = config.base_url + "/models"
                    self.assertEqual(provider_preflight_status(config).endpoint, endpoint)
                finally:
                    if previous is None:
                        os.environ.pop(key_name, None)
                    else:
                        os.environ[key_name] = previous
