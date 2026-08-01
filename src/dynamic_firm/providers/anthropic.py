from __future__ import annotations

import asyncio
import json
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping
from urllib.parse import urlsplit

from dynamic_firm import __version__
from dynamic_firm.providers.openai_compat import (
    EnvironmentSecretResolver,
    _completion_response_format,
    _http_failure,
    _is_loopback_host,
    _non_negative_int,
    _parse_completion,
)
from dynamic_firm.providers.wire_safety import sanitize_wire_payload
from dynamic_firm.runtime.models import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamProgress,
    StructuredOutputRequest,
    StructuredOutputResponse,
    ToolCall,
    Usage,
)
from dynamic_firm.runtime.ports import (
    CancellationToken,
    ModelProviderError,
    OperationCancelled,
)


@dataclass(frozen=True, slots=True)
class AnthropicProviderConfig:
    model: str
    base_url: str = "https://api.anthropic.com/v1"
    api_key_env: str = "ANTHROPIC_API_KEY"
    anthropic_version: str = "2023-06-01"
    max_tokens: int = 8_192
    timeout_seconds: float = 30.0
    max_response_bytes: int = 1_000_000
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    stream_responses: bool = False


class AnthropicProvider:
    """Bounded Anthropic Messages transport with optional SSE streaming."""

    def __init__(
        self,
        config: AnthropicProviderConfig,
        *,
        secret_resolver: EnvironmentSecretResolver | None = None,
    ) -> None:
        self.config = config
        self.secret_resolver = secret_resolver or EnvironmentSecretResolver()
        self.endpoint = self._validate_and_build_endpoint(config)

    @staticmethod
    def _validate_and_build_endpoint(config: AnthropicProviderConfig) -> str:
        if not config.model.strip() or not config.api_key_env.strip():
            raise ValueError("Anthropic model and credential environment name are required")
        if not config.anthropic_version.strip():
            raise ValueError("Anthropic API version must be non-empty")
        if config.max_tokens <= 0 or config.timeout_seconds <= 0 or config.max_response_bytes <= 0:
            raise ValueError("Anthropic token, timeout, and response limits must be positive")
        if config.input_cost_per_million < 0 or config.output_cost_per_million < 0:
            raise ValueError("Provider token prices cannot be negative")
        parsed = urlsplit(config.base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Provider base URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Provider base URL cannot contain credentials, query, or fragment")
        if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
            raise ValueError("Remote provider endpoints require HTTPS")
        return config.base_url.rstrip("/") + "/messages"

    async def complete(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        api_key = self.secret_resolver.resolve(self.config.api_key_env)
        return await self._await_network(
            self._complete_sync,
            request,
            api_key,
            cancellation=cancellation,
            task_name=f"anthropic:{request.run_id}:{request.call_index}",
        )

    async def complete_stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
        progress: Callable[[ModelStreamProgress], None],
    ) -> ModelResponse:
        if not self.config.stream_responses:
            return await self.complete(request, cancellation)
        cancellation.raise_if_cancelled()
        api_key = self.secret_resolver.resolve(self.config.api_key_env)
        loop = asyncio.get_running_loop()
        stopped = threading.Event()

        def publish(value: ModelStreamProgress) -> None:
            loop.call_soon_threadsafe(progress, value)

        network_task = asyncio.create_task(
            asyncio.to_thread(
                self._complete_stream_sync,
                request,
                api_key,
                publish,
                stopped,
            ),
            name=f"anthropic-stream:{request.run_id}:{request.call_index}",
        )
        cancel_task = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                {network_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done:
                stopped.set()
                network_task.cancel()
                await asyncio.gather(network_task, return_exceptions=True)
                raise OperationCancelled(cancellation.reason or "Run cancelled")
            return await network_task
        finally:
            stopped.set()
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    async def complete_structured(
        self,
        request: StructuredOutputRequest,
        cancellation: CancellationToken,
    ) -> StructuredOutputResponse:
        cancellation.raise_if_cancelled()
        if not request.schema_name.strip() or not request.messages or request.call_index < 1:
            raise ModelProviderError(
                "MODEL_REQUEST_INVALID",
                "Structured model request is missing required fields.",
                retryable=False,
            )
        api_key = self.secret_resolver.resolve(self.config.api_key_env)
        return await self._await_network(
            self._complete_structured_sync,
            request,
            api_key,
            cancellation=cancellation,
            task_name=f"anthropic-structured:{request.request_id}:{request.call_index}",
        )

    async def _await_network(
        self,
        sync_call,
        request,
        api_key: str,
        *,
        cancellation: CancellationToken,
        task_name: str,
    ):
        network_task = asyncio.create_task(
            asyncio.to_thread(sync_call, request, api_key),
            name=task_name,
        )
        cancel_task = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                {network_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done:
                network_task.cancel()
                await asyncio.gather(network_task, return_exceptions=True)
                raise OperationCancelled(cancellation.reason or "Run cancelled")
            return await network_task
        except asyncio.CancelledError:
            network_task.cancel()
            await asyncio.gather(network_task, return_exceptions=True)
            raise
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    def _complete_sync(self, request: ModelRequest, api_key: str) -> ModelResponse:
        payload = self._request_payload(request)
        return self._parse_response(self._post_payload(payload, api_key))

    def _request_payload(self, request: ModelRequest) -> dict:
        if len(request.tools) > 20:
            raise ModelProviderError(
                "MODEL_REQUEST_INVALID",
                "Anthropic strict tool count exceeds the bounded limit of 20.",
                retryable=False,
            )
        system, messages = _anthropic_messages(request.messages)
        completion_schema = _completion_response_format()["json_schema"]
        payload: dict = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": messages,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "strict": True,
                }
                for tool in request.tools
            ],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": completion_schema["schema"],
                }
            },
        }
        if system:
            payload["system"] = system
        return payload

    def _complete_stream_sync(
        self,
        request: ModelRequest,
        api_key: str,
        progress: Callable[[ModelStreamProgress], None],
        stopped: threading.Event,
    ) -> ModelResponse:
        payload = self._request_payload(request)
        payload["stream"] = True
        http_request = self._build_http_request(
            payload,
            api_key,
            accept="text/event-stream",
        )
        try:
            response = urllib.request.urlopen(
                http_request,
                timeout=self.config.timeout_seconds,
            )
        except urllib.error.HTTPError as exc:
            raise _http_failure(exc.code) from None
        except (TimeoutError, socket.timeout):
            raise ModelProviderError(
                "MODEL_TIMEOUT", "Model provider request timed out.", retryable=True
            ) from None
        except urllib.error.URLError:
            raise ModelProviderError(
                "MODEL_TRANSPORT_ERROR", "Model provider connection failed.", retryable=True
            ) from None

        blocks: dict[int, dict[str, object]] = {}
        usage: dict[str, int] = {}
        request_id: str | None = None
        stop_reason = "unknown"
        chunks = 0
        received_chars = 0
        total_bytes = 0
        raw_lines: list[bytes] = []
        saw_sse = False
        with response:
            for raw_line in response:
                if stopped.is_set():
                    raise OperationCancelled("Model stream cancelled")
                total_bytes += len(raw_line)
                if total_bytes > self.config.max_response_bytes:
                    raise ModelProviderError(
                        "MODEL_RESPONSE_TOO_LARGE",
                        "Model provider response exceeded the configured byte limit.",
                        retryable=False,
                    )
                raw_lines.append(raw_line)
                try:
                    line = raw_line.decode("utf-8", errors="strict").strip()
                except UnicodeDecodeError:
                    raise _invalid_response() from None
                if not line or line.startswith("event:"):
                    continue
                if not line.startswith("data:"):
                    continue
                saw_sse = True
                try:
                    event = json.loads(line[5:].strip())
                    sanitize_wire_payload(event)
                except (json.JSONDecodeError, ValueError):
                    raise _invalid_response() from None
                if not isinstance(event, dict):
                    raise _invalid_response()
                kind = event.get("type")
                if kind == "error":
                    error = event.get("error")
                    error_type = str(error.get("type", "")) if isinstance(error, dict) else ""
                    raise ModelProviderError(
                        "MODEL_RATE_LIMITED" if error_type == "overloaded_error" else "MODEL_UPSTREAM_ERROR",
                        "Model provider ended the stream with an error.",
                        retryable=True,
                    )
                if kind == "message_start":
                    message = event.get("message")
                    if isinstance(message, dict):
                        if message.get("id"):
                            request_id = str(message["id"])
                        if isinstance(message.get("usage"), dict):
                            usage.update(message["usage"])
                elif kind == "content_block_start":
                    index = int(event.get("index", 0) or 0)
                    block = event.get("content_block")
                    if not isinstance(block, dict):
                        raise _invalid_response()
                    if block.get("type") == "tool_use":
                        blocks[index] = {
                            "type": "tool_use",
                            "id": str(block.get("id") or ""),
                            "name": str(block.get("name") or ""),
                            "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                            "partial_json": "",
                        }
                    else:
                        blocks[index] = {
                            "type": "text",
                            "text": str(block.get("text") or ""),
                        }
                elif kind == "content_block_delta":
                    index = int(event.get("index", 0) or 0)
                    delta = event.get("delta")
                    if not isinstance(delta, dict):
                        raise _invalid_response()
                    target = blocks.setdefault(index, {"type": "text", "text": ""})
                    delta_chars = 0
                    if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                        target["text"] = str(target.get("text", "")) + delta["text"]
                        delta_chars = len(delta["text"])
                    elif delta.get("type") == "input_json_delta" and isinstance(
                        delta.get("partial_json"), str
                    ):
                        target["partial_json"] = str(target.get("partial_json", "")) + delta["partial_json"]
                        delta_chars = len(delta["partial_json"])
                    if delta_chars:
                        chunks += 1
                        received_chars += delta_chars
                        progress(ModelStreamProgress(chunks, received_chars))
                elif kind == "message_delta":
                    delta = event.get("delta")
                    if isinstance(delta, dict) and delta.get("stop_reason"):
                        stop_reason = str(delta["stop_reason"])
                    if isinstance(event.get("usage"), dict):
                        usage.update(event["usage"])

        if not saw_sse:
            try:
                ordinary = json.loads(b"".join(raw_lines).decode("utf-8"))
                sanitize_wire_payload(ordinary)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                raise _invalid_response() from None
            result = self._parse_response(ordinary)
            progress(ModelStreamProgress(1, len(result.content), True))
            return result

        assembled_blocks: list[dict[str, object]] = []
        for _, block in sorted(blocks.items()):
            if block.get("type") == "tool_use":
                partial = str(block.pop("partial_json", ""))
                if partial:
                    try:
                        parsed_input = json.loads(partial)
                    except json.JSONDecodeError:
                        raise _invalid_response() from None
                    block["input"] = parsed_input
            assembled_blocks.append(block)
        result = self._parse_response(
            {
                "id": request_id,
                "content": assembled_blocks,
                "stop_reason": stop_reason,
                "usage": usage,
            }
        )
        progress(ModelStreamProgress(chunks, received_chars, True))
        return result

    def _complete_structured_sync(
        self,
        request: StructuredOutputRequest,
        api_key: str,
    ) -> StructuredOutputResponse:
        system, messages = _anthropic_messages(request.messages)
        payload: dict = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": messages,
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": request.json_schema,
                }
            },
        }
        if system:
            payload["system"] = system
        return self._parse_structured_response(self._post_payload(payload, api_key))

    def _post_payload(self, payload: dict, api_key: str) -> dict:
        request = self._build_http_request(payload, api_key, accept="application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(self.config.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise _http_failure(exc.code) from None
        except (TimeoutError, socket.timeout):
            raise ModelProviderError(
                "MODEL_TIMEOUT", "Model provider request timed out.", retryable=True
            ) from None
        except urllib.error.URLError:
            raise ModelProviderError(
                "MODEL_TRANSPORT_ERROR", "Model provider connection failed.", retryable=True
            ) from None
        if len(raw) > self.config.max_response_bytes:
            raise ModelProviderError(
                "MODEL_RESPONSE_TOO_LARGE",
                "Model provider response exceeded the configured byte limit.",
                retryable=False,
            )
        try:
            value = json.loads(raw.decode("utf-8"))
            sanitize_wire_payload(value)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise _invalid_response() from None
        if not isinstance(value, dict):
            raise _invalid_response()
        return value

    def _build_http_request(
        self,
        payload: dict,
        api_key: str,
        *,
        accept: str,
    ) -> urllib.request.Request:
        try:
            sanitize_wire_payload(payload)
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, UnicodeEncodeError, ValueError):
            raise ModelProviderError(
                "MODEL_REQUEST_INVALID",
                "Model provider request could not be encoded safely.",
                retryable=False,
            ) from None
        return urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Accept": accept,
                "Content-Type": "application/json",
                "User-Agent": f"noruct/{__version__}",
                "Connection": "close",
                "x-api-key": api_key,
                "anthropic-version": self.config.anthropic_version,
            },
            method="POST",
        )

    def _usage(self, payload: Mapping[str, object]) -> Usage:
        raw = payload.get("usage")
        usage = raw if isinstance(raw, dict) else {}
        input_tokens = _non_negative_int(usage.get("input_tokens"))
        output_tokens = _non_negative_int(usage.get("output_tokens"))
        cache_read = _non_negative_int(usage.get("cache_read_input_tokens"))
        cost = (
            input_tokens * self.config.input_cost_per_million
            + output_tokens * self.config.output_cost_per_million
        ) / 1_000_000
        return Usage(
            input_tokens=input_tokens,
            cached_input_tokens=cache_read,
            output_tokens=output_tokens,
            cost_usd=round(cost, 12),
        )

    def _parse_response(self, payload: Mapping[str, object]) -> ModelResponse:
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise _invalid_response()
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in blocks:
            if not isinstance(block, dict):
                raise _invalid_response()
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif kind == "tool_use":
                call_id, name, arguments = block.get("id"), block.get("name"), block.get("input")
                if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                    raise _invalid_response()
                tool_calls.append(
                    ToolCall(
                        call_id=call_id,
                        name=name,
                        arguments=(
                            arguments
                            if isinstance(arguments, dict)
                            else {"_provider_arguments_error": "Arguments were not a JSON object."}
                        ),
                    )
                )
            else:
                raise _invalid_response()
        content = "".join(text_parts)
        stop_reason = str(payload.get("stop_reason") or "unknown")
        if not tool_calls and stop_reason in {"refusal", "max_tokens"}:
            raise ModelProviderError(
                "MODEL_STRUCTURED_OUTPUT_INVALID",
                "Model provider could not complete the required structured output.",
                retryable=stop_reason == "max_tokens",
            )
        return ModelResponse(
            content=content,
            tool_calls=tuple(tool_calls),
            completion=None if tool_calls else _parse_completion(content),
            usage=self._usage(payload),
            provider_request_id=str(payload["id"]) if payload.get("id") else None,
            finish_reason=stop_reason,
        )

    def _parse_structured_response(
        self,
        payload: Mapping[str, object],
    ) -> StructuredOutputResponse:
        blocks = payload.get("content")
        if not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(blocks[0], dict):
            raise _invalid_response()
        text = blocks[0].get("text") if blocks[0].get("type") == "text" else None
        if not isinstance(text, str):
            raise _invalid_response()
        stop_reason = str(payload.get("stop_reason") or "unknown")
        if stop_reason in {"refusal", "max_tokens"}:
            raise ModelProviderError(
                "MODEL_STRUCTURED_OUTPUT_INVALID",
                "Model provider could not complete the required structured output.",
                retryable=stop_reason == "max_tokens",
            )
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            raise ModelProviderError(
                "MODEL_STRUCTURED_OUTPUT_INVALID",
                "Model provider returned invalid structured output.",
                retryable=False,
            ) from None
        if not isinstance(value, dict):
            raise ModelProviderError(
                "MODEL_STRUCTURED_OUTPUT_INVALID",
                "Model provider structured output was not a JSON object.",
                retryable=False,
            )
        return StructuredOutputResponse(
            value=value,
            usage=self._usage(payload),
            provider_request_id=str(payload["id"]) if payload.get("id") else None,
            finish_reason=stop_reason,
        )


def _anthropic_messages(messages: tuple[ModelMessage, ...]) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    output: list[dict] = []
    for message in messages:
        if message.role == "system":
            system_parts.append(_content_text(message.content))
            continue
        if message.role == "tool":
            if not isinstance(message.tool_call_id, str) or not message.tool_call_id:
                raise ModelProviderError(
                    "MODEL_REQUEST_INVALID",
                    "Anthropic tool results require a non-empty tool use id.",
                    retryable=False,
                )
            output.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id,
                            "content": _content_text(message.content),
                        }
                    ],
                }
            )
            continue
        if message.role == "assistant" and isinstance(message.content, dict):
            blocks: list[dict] = []
            text = message.content.get("content")
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
            for call in message.content.get("tool_calls") or []:
                if isinstance(call, dict):
                    call_id = call.get("call_id")
                    name = call.get("name")
                    arguments = call.get("arguments")
                    if (
                        not isinstance(call_id, str)
                        or not call_id
                        or not isinstance(name, str)
                        or not name
                        or not isinstance(arguments, dict)
                    ):
                        raise ModelProviderError(
                            "MODEL_REQUEST_INVALID",
                            "Anthropic assistant tool history is invalid.",
                            retryable=False,
                        )
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": name,
                            "input": arguments,
                        }
                    )
            if blocks:
                output.append({"role": "assistant", "content": blocks})
            continue
        if message.role not in {"user", "assistant"}:
            raise ModelProviderError(
                "MODEL_REQUEST_INVALID",
                f"Anthropic message role is unsupported: {message.role}",
                retryable=False,
            )
        output.append({"role": message.role, "content": _content_text(message.content)})
    if not output:
        raise ModelProviderError(
            "MODEL_REQUEST_INVALID",
            "Anthropic request requires at least one user or assistant message.",
            retryable=False,
        )
    return "\n\n".join(system_parts), output


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _invalid_response() -> ModelProviderError:
    return ModelProviderError(
        "MODEL_RESPONSE_INVALID",
        "Model provider response did not match the Anthropic Messages contract.",
        retryable=True,
    )
