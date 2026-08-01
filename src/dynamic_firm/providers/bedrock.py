"""Bounded Amazon Bedrock Converse transport using a user-managed API key."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

from dynamic_firm import __version__
from dynamic_firm.runtime.models import CompletionEnvelope, ModelMessage, ModelRequest, ModelResponse, ToolCall, Usage
from dynamic_firm.runtime.ports import CancellationToken, ModelProviderError, OperationCancelled


@dataclass(frozen=True, slots=True)
class BedrockProviderConfig:
    base_url: str
    model: str
    api_key_env: str = "AWS_BEARER_TOKEN_BEDROCK"
    timeout_seconds: float = 30.0
    max_response_bytes: int = 1_000_000


class BedrockProvider:
    """Converse API with bounded JSON, fixed Bearer auth, and no AWS SDK."""

    def __init__(self, config: BedrockProviderConfig) -> None:
        parsed = urlsplit(config.base_url)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.startswith("bedrock-runtime.") or not parsed.hostname.endswith(".amazonaws.com"):
            raise ValueError("Bedrock base URL must be an HTTPS bedrock-runtime.<region>.amazonaws.com endpoint")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("Bedrock base URL must not contain a path, credentials, query, or fragment")
        if not config.model.strip() or not config.api_key_env.strip() or config.timeout_seconds <= 0:
            raise ValueError("Bedrock model, credential environment name, and timeout must be valid")
        self.config = config
        self.endpoint = config.base_url.rstrip("/") + "/model/" + quote(config.model, safe="") + "/converse"

    async def complete(self, request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        cancellation.raise_if_cancelled()
        token = os.environ.get(self.config.api_key_env, "")
        if not token:
            raise ModelProviderError("MODEL_SECRET_MISSING", f"Required model credential environment variable is not set: {self.config.api_key_env}", retryable=False)
        task = asyncio.create_task(asyncio.to_thread(self._complete_sync, request, token))
        cancelled = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait({task, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                task.cancel(); raise OperationCancelled(cancellation.reason or "Run cancelled")
            return await task
        finally:
            cancelled.cancel(); await asyncio.gather(cancelled, return_exceptions=True)

    def _complete_sync(self, request: ModelRequest, token: str) -> ModelResponse:
        system, messages = _messages(request.messages)
        payload: dict[str, Any] = {"messages": messages, "inferenceConfig": {"maxTokens": 20_000}}
        if system:
            payload["system"] = system
        if request.tools:
            payload["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": item.name,
                            "description": item.description,
                            "inputSchema": {"json": dict(item.input_schema)},
                        }
                    }
                    for item in request.tools
                ]
            }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        http = urllib.request.Request(self.endpoint, data=raw, method="POST", headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {token}", "User-Agent": f"noruct/{__version__}"})
        try:
            with urllib.request.urlopen(http, timeout=self.config.timeout_seconds) as response:
                body = response.read(self.config.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise ModelProviderError("MODEL_REQUEST_REJECTED", f"Model provider rejected the request with HTTP {exc.code}.", retryable=exc.code in {408, 429, 500, 502, 503, 504}) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            raise ModelProviderError("MODEL_TRANSPORT_ERROR", "Model provider connection failed.", retryable=True) from None
        if len(body) > self.config.max_response_bytes:
            raise ModelProviderError("MODEL_RESPONSE_TOO_LARGE", "Model provider response exceeded the configured byte limit.", retryable=False)
        try:
            response = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ModelProviderError("MODEL_RESPONSE_INVALID", "Model provider returned invalid JSON.", retryable=True) from None
        return _response(response)


def _text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _messages(source: tuple[ModelMessage, ...]) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    system: list[dict[str, str]] = []; result: list[dict[str, object]] = []
    def append(role: str, blocks: list[dict[str, object]]) -> None:
        if result and result[-1]["role"] == role:
            result[-1]["content"].extend(blocks)  # type: ignore[index]
        else: result.append({"role": role, "content": blocks})
    for message in source:
        if message.role == "system":
            system.append({"text": _text(message.content) or " "}); continue
        if message.role == "tool":
            append("user", [{"toolResult": {"toolUseId": message.tool_call_id or "", "content": [{"text": _text(message.content) or " "}]}}]); continue
        if message.role == "assistant" and isinstance(message.content, Mapping):
            blocks: list[dict[str, object]] = []
            content = message.content.get("content")
            if content: blocks.append({"text": _text(content)})
            for call in message.content.get("tool_calls", ()):
                if isinstance(call, Mapping): blocks.append({"toolUse": {"toolUseId": str(call.get("call_id", "")), "name": str(call.get("name", "")), "input": dict(call.get("arguments", {})) if isinstance(call.get("arguments"), Mapping) else {}}})
            append("assistant", blocks or [{"text": " "}]); continue
        append("assistant" if message.role == "assistant" else "user", [{"text": _text(message.content) or " "}])
    if not result or result[0]["role"] != "user": result.insert(0, {"role": "user", "content": [{"text": " "}]})
    if result[-1]["role"] != "user": result.append({"role": "user", "content": [{"text": " "}]})
    return system, result


def _response(payload: object) -> ModelResponse:
    if not isinstance(payload, Mapping): raise ModelProviderError("MODEL_RESPONSE_INVALID", "Model provider response did not match the Converse contract.", retryable=True)
    blocks = ((payload.get("output") or {}).get("message") or {}).get("content", []) if isinstance(payload.get("output"), Mapping) else []
    if not isinstance(blocks, list): raise ModelProviderError("MODEL_RESPONSE_INVALID", "Model provider response did not match the Converse contract.", retryable=True)
    text = []; calls = []
    for block in blocks:
        if not isinstance(block, Mapping): continue
        if isinstance(block.get("text"), str): text.append(block["text"])
        tool = block.get("toolUse")
        if isinstance(tool, Mapping): calls.append(ToolCall(str(tool.get("toolUseId", "")), str(tool.get("name", "")), dict(tool.get("input", {})) if isinstance(tool.get("input"), Mapping) else {}))
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
    content = "\n".join(text)
    completion = None
    if not calls:
        try:
            structured = json.loads(content)
        except json.JSONDecodeError:
            structured = None
        if isinstance(structured, Mapping) and isinstance(structured.get("summary"), str):
            completion = CompletionEnvelope(summary=structured["summary"])
    return ModelResponse(content=content, tool_calls=tuple(calls), completion=completion, usage=Usage(input_tokens=int(usage.get("inputTokens", 0) or 0), output_tokens=int(usage.get("outputTokens", 0) or 0)), finish_reason="tool_calls" if calls else str(payload.get("stopReason", "stop")))
