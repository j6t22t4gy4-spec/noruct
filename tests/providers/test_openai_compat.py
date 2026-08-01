from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dynamic_firm.providers.openai_compat import (
    EnvironmentSecretResolver,
    OpenAICompatProvider,
    OpenAICompatProviderConfig,
)
from dynamic_firm.runtime.models import (
    ModelMessage,
    ModelRequest,
    RunStatus,
    SignalCode,
    StructuredOutputRequest,
    ToolSchema,
)
from dynamic_firm.runtime.ports import CancellationToken, ModelProviderError
from dynamic_firm.runtime.secrets import employee_secret_scope
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry
from tests.runtime.helpers import make_request


class _ContractHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.captures.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "api_key": self.headers.get("api-key"),
                "body": json.loads(body.decode("utf-8")),
            }
        )
        spec = self.server.responses.pop(0)
        delay = spec.get("delay", 0)
        if delay:
            time.sleep(delay)
        payload = spec.get("raw")
        if payload is None:
            payload = json.dumps(spec.get("json", {})).encode("utf-8")
        self.send_response(spec.get("status", 200))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except BrokenPipeError:
            pass

    def log_message(self, format: str, *args) -> None:
        return


@contextmanager
def contract_server(*responses):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ContractHandler)
    server.responses = list(responses)
    server.captures = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def model_request(*messages: ModelMessage, tools=()) -> ModelRequest:
    return ModelRequest(
        messages=tuple(messages),
        tools=tuple(tools),
        model_profile="contract-model",
        run_id="run-contract",
        call_index=1,
    )


def completion_body(*, summary: str = "Complete", signals=()) -> dict:
    content = {
        "summary": summary,
        "artifact_refs": [],
        "acceptance_evidence": ["fixture:evidence"],
        "unresolved_issues": [],
        "suggested_followups": [],
        "observations": [],
        "signals": list(signals),
    }
    return {
        "id": "chatcmpl-contract",
        "choices": [
            {
                "message": {"role": "assistant", "content": json.dumps(content)},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 3},
        },
    }


class OpenAICompatProviderTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, base_url: str, **changes) -> OpenAICompatProvider:
        config = OpenAICompatProviderConfig(
            base_url=base_url,
            model="contract-model",
            timeout_seconds=changes.pop("timeout_seconds", 1),
            max_response_bytes=changes.pop("max_response_bytes", 10_000),
            **changes,
        )
        return OpenAICompatProvider(
            config,
            secret_resolver=EnvironmentSecretResolver({"TEST_MODEL_KEY": "fixture-secret"}),
        )

    async def test_serializes_tools_messages_and_parses_tool_call_usage(self) -> None:
        response = {
            "id": "chatcmpl-tool",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "read_fixture",
                                    "arguments": "{\"key\":\"bug\"}",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 4,
                "prompt_tokens_details": {"cached_tokens": 5},
            },
        }
        with contract_server({"json": response}) as (server, base_url):
            provider = self.provider(
                base_url,
                api_key_env="TEST_MODEL_KEY",
                input_cost_per_million=1.0,
                output_cost_per_million=2.0,
            )
            request = model_request(
                ModelMessage("system", "system"),
                ModelMessage(
                    "assistant",
                    {
                        "content": "",
                        "tool_calls": [
                            {"call_id": "previous", "name": "read_fixture", "arguments": {"key": "a"}}
                        ],
                    },
                ),
                ModelMessage("tool", {"ok": True, "content": "evidence"}, "previous"),
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
            )

            result = await provider.complete(request, CancellationToken())

        self.assertEqual(result.tool_calls[0].arguments, {"key": "bug"})
        self.assertEqual(result.usage.input_tokens, 20)
        self.assertEqual(result.usage.cached_input_tokens, 5)
        self.assertEqual(result.usage.output_tokens, 4)
        self.assertEqual(result.usage.cost_usd, 0.000028)
        captured = server.captures[0]
        self.assertEqual(captured["path"], "/v1/chat/completions")
        self.assertEqual(captured["authorization"], "Bearer fixture-secret")
        self.assertEqual(captured["body"]["tools"][0]["function"]["name"], "read_fixture")
        self.assertEqual(captured["body"]["messages"][2]["tool_call_id"], "previous")
        self.assertEqual(captured["body"]["response_format"]["type"], "json_schema")
        self.assertFalse(captured["body"]["stream"])

    async def test_sse_stream_accumulates_structured_completion_and_reports_progress(self) -> None:
        # completion_body content is already JSON text; split that text itself.
        content = completion_body(summary="Streamed")["choices"][0]["message"]["content"]
        midpoint = len(content) // 2
        events = (
            {"id": "stream-contract", "choices": [{"delta": {"content": content[:midpoint]}, "finish_reason": None}]},
            {"id": "stream-contract", "choices": [{"delta": {"content": content[midpoint:]}, "finish_reason": "stop"}]},
            {"id": "stream-contract", "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 6}},
        )
        raw = b"".join(
            f"data: {json.dumps(event)}\n\n".encode("utf-8") for event in events
        ) + b"data: [DONE]\n\n"
        with contract_server({"raw": raw}) as (server, base_url):
            provider = self.provider(
                base_url,
                api_key_env="TEST_MODEL_KEY",
                stream_responses=True,
            )
            progress = []
            result = await provider.complete_stream(
                model_request(ModelMessage("user", "Inspect")),
                CancellationToken(),
                progress.append,
            )
            await asyncio.sleep(0)

        self.assertEqual(result.completion.summary, "Streamed")
        self.assertEqual(result.usage.input_tokens, 9)
        self.assertTrue(progress[-1].finished)
        self.assertEqual(progress[-1].received_chars, len(content))
        self.assertTrue(server.captures[0]["body"]["stream"])
        self.assertTrue(server.captures[0]["body"]["stream_options"]["include_usage"])

    async def test_fixed_api_key_header_variant_never_sends_bearer_authorization(self) -> None:
        with contract_server({"json": completion_body(summary="Azure connected")}) as (server, base_url):
            provider = self.provider(
                base_url,
                api_key_env="TEST_MODEL_KEY",
                credential_header="api-key",
                credential_prefix="",
            )
            result = await provider.complete(
                model_request(ModelMessage("user", "Inspect")),
                CancellationToken(),
            )

        self.assertEqual(result.completion.summary, "Azure connected")
        self.assertEqual(server.captures[0]["api_key"], "fixture-secret")
        self.assertIsNone(server.captures[0]["authorization"])

    async def test_parses_structured_completion_and_signal(self) -> None:
        signal = {
            "code": "CAPABILITY_MISSING",
            "value": "database_migration",
            "evidence": ["schema.py:4"],
        }
        with contract_server({"json": completion_body(signals=(signal,))}) as (_, base_url):
            provider = self.provider(base_url, api_key_env="TEST_MODEL_KEY")
            result = await provider.complete(
                model_request(ModelMessage("user", "Inspect")),
                CancellationToken(),
            )

        self.assertEqual(result.completion.summary, "Complete")
        self.assertEqual(result.completion.signals[0].code, SignalCode.CAPABILITY_MISSING)
        self.assertEqual(result.provider_request_id, "chatcmpl-contract")

    async def test_parses_typed_semantic_replan_without_granting_patch_authority(self) -> None:
        signal = {
            "code": "ASSUMPTION_INVALIDATED",
            "value": "",
            "evidence": ["evidence:assumption-window"],
            "semantic_replan": {
                "operation": "SPLIT",
                "task_ids": [],
                "capability_ids": ["research", "verification"],
                "assumption_refs": ["evidence:assumption-window"],
                "constraint_refs": [],
            },
        }
        with contract_server({"json": completion_body(signals=(signal,))}) as (_, base_url):
            provider = self.provider(base_url, api_key_env="TEST_MODEL_KEY")
            result = await provider.complete(
                model_request(ModelMessage("user", "Inspect")),
                CancellationToken(),
            )

        directive = result.completion.signals[0].semantic_replan
        self.assertIsNotNone(directive)
        assert directive is not None
        self.assertEqual(directive.operation.value, "SPLIT")
        self.assertEqual(directive.capability_ids, ("research", "verification"))

    async def test_drops_malformed_semantic_replan_but_preserves_signal(self) -> None:
        signal = {
            "code": "ASSUMPTION_INVALIDATED",
            "value": "split:research,verification",
            "evidence": ["evidence:assumption-window"],
            "semantic_replan": {"operation": "SPLIT", "capability_ids": "not-a-list"},
        }
        with contract_server({"json": completion_body(signals=(signal,))}) as (_, base_url):
            provider = self.provider(base_url, api_key_env="TEST_MODEL_KEY")
            result = await provider.complete(
                model_request(ModelMessage("user", "Inspect")),
                CancellationToken(),
            )

        self.assertEqual(result.completion.signals[0].code, SignalCode.ASSUMPTION_INVALIDATED)
        self.assertIsNone(result.completion.signals[0].semantic_replan)

    async def test_generic_structured_output_uses_caller_schema_and_normalizes_usage(self) -> None:
        plan_value = {"mode": "SOLO"}
        response = {
            "id": "chatcmpl-plan",
            "choices": [
                {
                    "message": {"role": "assistant", "content": json.dumps(plan_value)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 8, "completion_tokens": 3},
        }
        schema = {
            "type": "object",
            "properties": {"mode": {"type": "string"}},
            "required": ["mode"],
            "additionalProperties": False,
        }
        with contract_server({"json": response}) as (server, base_url):
            provider = self.provider(base_url, api_key_env="TEST_MODEL_KEY")
            result = await provider.complete_structured(
                StructuredOutputRequest(
                    messages=(ModelMessage("user", "Plan"),),
                    schema_name="company_plan",
                    json_schema=schema,
                    model_profile="contract-model",
                    request_id="plan-request",
                ),
                CancellationToken(),
            )

        self.assertEqual(result.value, plan_value)
        self.assertEqual(result.usage.input_tokens, 8)
        self.assertEqual(result.usage.output_tokens, 3)
        response_format = server.captures[0]["body"]["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(response_format["json_schema"]["name"], "company_plan")
        self.assertEqual(response_format["json_schema"]["schema"], schema)
        self.assertNotIn("tools", server.captures[0]["body"])

    async def test_generic_structured_output_rejects_non_json_content(self) -> None:
        response = {
            "choices": [
                {"message": {"role": "assistant", "content": "not json"}, "finish_reason": "stop"}
            ]
        }
        with contract_server({"json": response}) as (_, base_url):
            provider = self.provider(base_url, api_key_env="TEST_MODEL_KEY")
            with self.assertRaises(ModelProviderError) as raised:
                await provider.complete_structured(
                    StructuredOutputRequest(
                        messages=(ModelMessage("user", "Plan"),),
                        schema_name="company_plan",
                        json_schema={"type": "object"},
                        model_profile="contract-model",
                        request_id="plan-request",
                    ),
                    CancellationToken(),
                )
        self.assertEqual(raised.exception.code, "MODEL_STRUCTURED_OUTPUT_INVALID")

    async def test_sanitizes_request_surrogates_and_repairs_provider_tool_arguments(self) -> None:
        response = {
            "id": "chatcmpl-repaired-tool",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-repaired",
                                "type": "function",
                                "function": {
                                    "name": "read_fixture",
                                    "arguments": '{"key":"bug",}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        with contract_server({"json": response}) as (server, base_url):
            provider = self.provider(base_url, api_key_env="TEST_MODEL_KEY")
            result = await provider.complete(
                model_request(ModelMessage("user", "clipboard \udce2 text")),
                CancellationToken(),
            )

        self.assertEqual(result.tool_calls[0].arguments, {"key": "bug"})
        self.assertEqual(server.captures[0]["body"]["messages"][0]["content"], "clipboard � text")

    async def test_parallel_provider_calls_use_their_employee_secret_scopes(self) -> None:
        async def scoped_call(provider: OpenAICompatProvider, secret: str) -> None:
            with employee_secret_scope({"SCOPED_MODEL_KEY": secret}):
                await asyncio.sleep(0)
                await provider.complete(
                    model_request(ModelMessage("user", "Inspect")),
                    CancellationToken(),
                )

        with contract_server(
            {"json": completion_body(summary="First")},
            {"json": completion_body(summary="Second")},
        ) as (server, base_url):
            provider = OpenAICompatProvider(
                OpenAICompatProviderConfig(
                    base_url=base_url,
                    model="contract-model",
                    api_key_env="SCOPED_MODEL_KEY",
                )
            )
            await asyncio.gather(
                scoped_call(provider, "employee-a-secret"),
                scoped_call(provider, "employee-b-secret"),
            )

        authorizations = {capture["authorization"] for capture in server.captures}
        self.assertEqual(
            authorizations,
            {"Bearer employee-a-secret", "Bearer employee-b-secret"},
        )

    async def test_maps_http_and_protocol_failures_without_response_body(self) -> None:
        cases = (
            ({"status": 401, "raw": b'{"secret":"echo"}'}, "MODEL_AUTH_FAILED", False),
            ({"status": 429, "raw": b"rate"}, "MODEL_RATE_LIMITED", True),
            ({"status": 500, "raw": b"upstream"}, "MODEL_UPSTREAM_ERROR", True),
            ({"raw": b"not-json"}, "MODEL_RESPONSE_INVALID", True),
            ({"json": {"choices": []}}, "MODEL_RESPONSE_INVALID", True),
        )
        for response, code, retryable in cases:
            with self.subTest(code=code), contract_server(response) as (_, base_url):
                provider = self.provider(base_url, api_key_env="TEST_MODEL_KEY")
                with self.assertRaises(ModelProviderError) as raised:
                    await provider.complete(
                        model_request(ModelMessage("user", "Inspect")),
                        CancellationToken(),
                    )
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertNotIn("echo", str(raised.exception))
                self.assertNotIn("fixture-secret", str(raised.exception))

    async def test_enforces_timeout_and_response_byte_limit(self) -> None:
        with contract_server({"delay": 0.1, "json": completion_body()}) as (_, base_url):
            provider = self.provider(
                base_url,
                api_key_env="TEST_MODEL_KEY",
                timeout_seconds=0.01,
            )
            with self.assertRaises(ModelProviderError) as raised:
                await provider.complete(
                    model_request(ModelMessage("user", "Inspect")),
                    CancellationToken(),
                )
            self.assertEqual(raised.exception.code, "MODEL_TIMEOUT")

        with contract_server({"raw": b"x" * 101}) as (_, base_url):
            provider = self.provider(
                base_url,
                api_key_env="TEST_MODEL_KEY",
                max_response_bytes=100,
            )
            with self.assertRaises(ModelProviderError) as raised:
                await provider.complete(
                    model_request(ModelMessage("user", "Inspect")),
                    CancellationToken(),
                )
            self.assertEqual(raised.exception.code, "MODEL_RESPONSE_TOO_LARGE")

    async def test_runtime_persists_result_without_api_key(self) -> None:
        secret = "must-not-enter-runtime-store"
        with contract_server({"json": completion_body(summary="Runtime connected")}) as (_, base_url):
            provider = OpenAICompatProvider(
                OpenAICompatProviderConfig(
                    base_url=base_url,
                    model="contract-model",
                    api_key_env="TEST_MODEL_KEY",
                ),
                secret_resolver=EnvironmentSecretResolver({"TEST_MODEL_KEY": secret}),
            )
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "runtime.db"
                store = RunStore(path)
                service = NativeEmployeeRuntimeService(
                    store=store,
                    provider=provider,
                    registry=ToolRegistry(),
                )

                result = await service.collect(await service.start(make_request()))
                store.close()
                database_bytes = path.read_bytes()

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.summary, "Runtime connected")
        self.assertNotIn(secret.encode("utf-8"), database_bytes)

    def test_rejects_remote_plain_http_and_url_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "require HTTPS"):
            OpenAICompatProvider(OpenAICompatProviderConfig("http://example.com/v1", "model"))
        with self.assertRaisesRegex(ValueError, "cannot contain credentials"):
            OpenAICompatProvider(
                OpenAICompatProviderConfig("https://user:pass@example.com/v1", "model")
            )
        with self.assertRaisesRegex(ValueError, "safe HTTP header name"):
            OpenAICompatProvider(
                OpenAICompatProviderConfig(
                    "https://example.com/v1", "model", credential_header="api-key\r\nX-Injected"
                )
            )


if __name__ == "__main__":
    unittest.main()
