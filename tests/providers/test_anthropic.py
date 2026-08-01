from __future__ import annotations

import asyncio
import json
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dynamic_firm.providers.anthropic import AnthropicProvider, AnthropicProviderConfig
from dynamic_firm.providers.openai_compat import EnvironmentSecretResolver
from dynamic_firm.runtime.models import (
    ModelMessage,
    ModelRequest,
    StructuredOutputRequest,
    ToolSchema,
)
from dynamic_firm.runtime.ports import CancellationToken, ModelProviderError


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.captures.append(
            {
                "path": self.path,
                "api_key": self.headers.get("x-api-key"),
                "version": self.headers.get("anthropic-version"),
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(body),
            }
        )
        response = self.server.responses.pop(0)
        raw = response.get("raw")
        if raw is None:
            raw = json.dumps(response.get("json", {})).encode()
        self.send_response(response.get("status", 200))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args) -> None:
        return


@contextmanager
def server(*responses):
    instance = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    instance.responses = list(responses)
    instance.captures = []
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance, f"http://127.0.0.1:{instance.server_port}/v1"
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=1)


class AnthropicProviderTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, base_url: str) -> AnthropicProvider:
        return AnthropicProvider(
            AnthropicProviderConfig(
                model="claude-contract",
                base_url=base_url,
                api_key_env="ANTHROPIC_TEST_KEY",
                timeout_seconds=1,
            ),
            secret_resolver=EnvironmentSecretResolver(
                {"ANTHROPIC_TEST_KEY": "anthropic-fixture-secret"}
            ),
        )

    async def test_native_messages_round_trip_preserves_tool_use_contract(self) -> None:
        response = {
            "id": "msg-contract",
            "type": "message",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-next",
                    "name": "read_fixture",
                    "input": {"key": "bug"},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {
                "input_tokens": 20,
                "cache_read_input_tokens": 5,
                "output_tokens": 4,
            },
        }
        with server({"json": response}) as (instance, base_url):
            result = await self.provider(base_url).complete(
                ModelRequest(
                    messages=(
                        ModelMessage("system", "system policy"),
                        ModelMessage("user", "inspect"),
                        ModelMessage(
                            "assistant",
                            {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "call_id": "toolu-prior",
                                        "name": "read_fixture",
                                        "arguments": {"key": "prior"},
                                    }
                                ],
                            },
                        ),
                        ModelMessage(
                            "tool",
                            {"ok": True, "content": "evidence"},
                            "toolu-prior",
                        ),
                    ),
                    tools=(
                        ToolSchema(
                            "read_fixture",
                            "Read fixture",
                            {
                                "type": "object",
                                "properties": {"key": {"type": "string"}},
                                "required": ["key"],
                                "additionalProperties": False,
                            },
                        ),
                    ),
                    model_profile="contract",
                    run_id="run-contract",
                    call_index=1,
                ),
                CancellationToken(),
            )

        self.assertEqual(result.tool_calls[0].arguments, {"key": "bug"})
        self.assertEqual(result.usage.cached_input_tokens, 5)
        capture = instance.captures[0]
        self.assertEqual(capture["path"], "/v1/messages")
        self.assertEqual(capture["api_key"], "anthropic-fixture-secret")
        self.assertEqual(capture["version"], "2023-06-01")
        self.assertIsNone(capture["authorization"])
        self.assertEqual(capture["body"]["system"], "system policy")
        self.assertEqual(capture["body"]["tools"][0]["name"], "read_fixture")
        self.assertEqual(
            capture["body"]["messages"][1]["content"][0]["type"],
            "tool_use",
        )
        self.assertEqual(
            capture["body"]["messages"][2]["content"][0]["type"],
            "tool_result",
        )

    async def test_sse_stream_accumulates_structured_text_and_reports_progress(self) -> None:
        content = json.dumps(
            {
                "summary": "Anthropic streamed",
                "artifact_refs": [],
                "acceptance_evidence": ["fixture:evidence"],
                "unresolved_issues": [],
                "suggested_followups": [],
                "observations": [],
                "signals": [],
            }
        )
        midpoint = len(content) // 2
        events = (
            {"type": "message_start", "message": {"id": "msg-stream", "usage": {"input_tokens": 12}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": content[:midpoint]}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": content[midpoint:]}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}},
            {"type": "message_stop"},
        )
        raw = b"".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode("utf-8")
            for event in events
        )
        with server({"raw": raw}) as (instance, base_url):
            provider = AnthropicProvider(
                AnthropicProviderConfig(
                    model="claude-contract",
                    base_url=base_url,
                    api_key_env="ANTHROPIC_TEST_KEY",
                    timeout_seconds=1,
                    stream_responses=True,
                ),
                secret_resolver=EnvironmentSecretResolver(
                    {"ANTHROPIC_TEST_KEY": "anthropic-fixture-secret"}
                ),
            )
            progress = []
            result = await provider.complete_stream(
                ModelRequest(
                    messages=(ModelMessage("user", "Inspect"),),
                    tools=(),
                    model_profile="contract",
                    run_id="run-stream",
                    call_index=1,
                ),
                CancellationToken(),
                progress.append,
            )
            await asyncio.sleep(0)

        self.assertEqual(result.completion.summary, "Anthropic streamed")
        self.assertEqual(result.usage.input_tokens, 12)
        self.assertEqual(result.usage.output_tokens, 7)
        self.assertTrue(progress[-1].finished)
        self.assertEqual(progress[-1].received_chars, len(content))
        self.assertTrue(instance.captures[0]["body"]["stream"])

    async def test_structured_output_uses_caller_schema_and_parses_usage(self) -> None:
        response = {
            "id": "msg-plan",
            "content": [{"type": "text", "text": '{"mode":"SOLO"}'}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 8, "output_tokens": 3},
        }
        schema = {
            "type": "object",
            "properties": {"mode": {"type": "string"}},
            "required": ["mode"],
            "additionalProperties": False,
        }
        with server({"json": response}) as (instance, base_url):
            result = await self.provider(base_url).complete_structured(
                StructuredOutputRequest(
                    messages=(ModelMessage("user", "plan"),),
                    schema_name="company_plan",
                    json_schema=schema,
                    model_profile="contract",
                    request_id="plan-contract",
                ),
                CancellationToken(),
            )
        self.assertEqual(result.value, {"mode": "SOLO"})
        self.assertEqual(result.usage.input_tokens, 8)
        format_payload = instance.captures[0]["body"]["output_config"]["format"]
        self.assertEqual(format_payload["type"], "json_schema")
        self.assertEqual(format_payload["schema"], schema)

    async def test_http_auth_failure_is_safe(self) -> None:
        with server({"status": 401, "json": {"error": {"message": "secret echo"}}}) as (
            _,
            base_url,
        ):
            with self.assertRaises(ModelProviderError) as raised:
                await self.provider(base_url).complete(
                    ModelRequest(
                        messages=(ModelMessage("user", "inspect"),),
                        tools=(),
                        model_profile="contract",
                        run_id="run-contract",
                        call_index=1,
                    ),
                    CancellationToken(),
                )
        self.assertEqual(raised.exception.code, "MODEL_AUTH_FAILED")
        self.assertNotIn("secret echo", str(raised.exception))
        self.assertNotIn("anthropic-fixture-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
