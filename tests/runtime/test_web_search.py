from __future__ import annotations

import asyncio
import io
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dynamic_firm.cli import (
    EXIT_OK,
    RunCommandConfig,
    _action_policy,
    _resolve_foundation_runtime_python,
    main,
    run_goal,
)
from dynamic_firm.product.routing import InputRoute
from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.models import (
    ApprovalDecision,
    CompletionEnvelope,
    ModelResponse,
    RunLimits,
    ToolCall,
    ToolEffect,
)
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.tools import ToolValidationError
from dynamic_firm.web_search import WEB_SEARCH_TOOL, SearxngSearchConfig, SearxngSearchConnector, config_from_settings


class _SearchHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib callback
        payload = json.dumps({"results": [
            {"title": "Second", "url": "https://example.org/second", "content": "second result", "score": 0.2},
            {"title": "First", "url": "https://example.org/first", "content": "first result", "score": 0.9},
        ]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return None


class _AllowOnce:
    def __init__(self) -> None:
        self.requests = []

    async def request(self, request, cancellation):
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        return ApprovalDecision.ALLOW_ONCE


class WebSearchTests(unittest.TestCase):
    def test_config_rejects_unbounded_public_http_and_unknown_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            SearxngSearchConfig("http://search.example").validate()
        self.assertIsNone(config_from_settings({}))
        with self.assertRaisesRegex(ValueError, "Unknown"):
            config_from_settings({"web_search": {"enabled": True, "base_url": "https://search.example", "token": "no"}})

    def test_loopback_search_normalizes_bounded_untrusted_results(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SearchHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            config = SearxngSearchConfig(f"http://127.0.0.1:{server.server_port}", max_results=2)
            definition = SearxngSearchConnector(config).definition()
            self.assertEqual(definition.name, WEB_SEARCH_TOOL)
            self.assertEqual(definition.effect, ToolEffect.NETWORK)
            result = asyncio.run(definition.handler(definition.validator({"query": "noruct"}), CancellationToken()))
            payload = json.loads(result)
            self.assertEqual(payload["results"][0]["title"], "First")
            self.assertIn("untrusted", payload["trust"])
            with self.assertRaises(ToolValidationError):
                definition.validator({"query": "x", "max_results": 3})
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_cli_configure_and_policy_grant_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "noruct.toml"
            output = io.StringIO()
            self.assertEqual(main(["--config", str(config_path), "web-search", "configure", "--base-url", "http://127.0.0.1:8080"], stdout=output, stderr=io.StringIO()), EXIT_OK)
            self.assertIn("SearXNG web-search capability: ready", output.getvalue())
            settings = __import__("tomllib").loads(config_path.read_text(encoding="utf-8"))
            config = config_from_settings(settings)
            assert config is not None
            from dynamic_firm.cli import RunCommandConfig
            run = RunCommandConfig(goal="x", workspace=Path(directory), state_path=Path(directory) / "runtime.db", provider_kind="openai_api", base_url="https://api.example/v1", model="model", codex_model=None, codex_command="", api_key_env="KEY", request_timeout_seconds=10, permission_mode="read-only", run_limits=RunLimits(), web_search=config)
            grant = next(item for item in _action_policy(run).tool_grants if item.tool_name == WEB_SEARCH_TOOL)
            self.assertEqual(grant.allowed_effects, (ToolEffect.NETWORK,))
            approval_grant = next(
                item for item in _action_policy(replace(run, external_read_mode="ask")).tool_grants
                if item.tool_name == WEB_SEARCH_TOOL
            )
            self.assertTrue(approval_grant.requires_approval)
            blocked_names = {
                item.tool_name for item in _action_policy(replace(run, external_read_mode="blocked")).tool_grants
            }
            self.assertNotIn(WEB_SEARCH_TOOL, blocked_names)

    def test_direct_runtime_honors_external_read_ask_after_cli_policy_assembly(self) -> None:
        """A Settings-created ask policy must reach its approval, not reject NETWORK grants."""

        server = ThreadingHTTPServer(("127.0.0.1", 0), _SearchHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                provider = ScriptedModelProvider(
                    (
                        ModelResponse(
                            tool_calls=(
                                ToolCall(
                                    "direct-search-1",
                                    WEB_SEARCH_TOOL,
                                    {"query": "noruct"},
                                ),
                            ),
                            finish_reason="tool_calls",
                        ),
                        ModelResponse(
                            completion=CompletionEnvelope(
                                summary="Search evidence reviewed."
                            )
                        ),
                    )
                )
                config = RunCommandConfig(
                    goal="Could you look this up?",
                    workspace=root,
                    state_path=root / "runtime.db",
                    provider_kind="openai_api",
                    base_url="https://unused.invalid/v1",
                    model="scripted",
                    codex_model=None,
                    codex_command="codex",
                    api_key_env=None,
                    request_timeout_seconds=5,
                    permission_mode="read-only",
                    run_limits=RunLimits(max_model_calls=2, max_tool_calls=3),
                    web_search=SearxngSearchConfig(
                        f"http://127.0.0.1:{server.server_port}",
                    ),
                    external_read_mode="ask",
                    runtime_python=_resolve_foundation_runtime_python(""),
                )
                approval = _AllowOnce()
                result = asyncio.run(
                    run_goal(
                        config,
                        provider,
                        approval_port=approval,
                        route=InputRoute.CONVERSATION,
                    )
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result.status.value, "SUCCEEDED")
        self.assertEqual(result.summary, "Search evidence reviewed.")
        self.assertEqual([item.tool_name for item in approval.requests], [WEB_SEARCH_TOOL])
