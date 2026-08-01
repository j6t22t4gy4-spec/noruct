from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping
from urllib.parse import urlsplit

from dynamic_firm import __version__
from dynamic_firm.providers.wire_safety import parse_tool_arguments, sanitize_wire_payload
from dynamic_firm.runtime.models import (
    CompletionEnvelope,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamProgress,
    RunSignal,
    SemanticReplanOperation,
    SignalCode,
    StructuredOutputRequest,
    StructuredOutputResponse,
    ToolCall,
    Usage,
    semantic_replan_directive_from_dict,
)
from dynamic_firm.runtime.ports import (
    CancellationToken,
    ModelProviderError,
    OperationCancelled,
)
from dynamic_firm.runtime.secrets import SecretScopeError, resolve_secret


@dataclass(frozen=True, slots=True)
class OpenAICompatProviderConfig:
    base_url: str
    model: str
    api_key_env: str | None = "NORUCT_API_KEY"
    timeout_seconds: float = 30.0
    max_response_bytes: int = 1_000_000
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    stream_responses: bool = False
    stream_include_usage: bool = True
    # Most OpenAI-compatible services use ``Authorization: Bearer``.  A
    # small number of documented compatible endpoints use a named API-key
    # header instead.  Keep this as transport metadata, never Company state
    # or a user-provided raw header/value escape hatch.
    credential_header: str = "Authorization"
    credential_prefix: str = "Bearer "


class EnvironmentSecretResolver:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ

    def resolve(self, name: str) -> str:
        try:
            value = self._environ.get(name, "") if self._environ is not None else resolve_secret(name, "")
        except SecretScopeError:
            raise ModelProviderError(
                "MODEL_SECRET_SCOPE_MISSING",
                "A model credential was requested outside its employee execution scope.",
                retryable=False,
            ) from None
        if not value:
            raise ModelProviderError(
                "MODEL_SECRET_MISSING",
                f"Required model credential environment variable is not set: {name}",
                retryable=False,
            )
        return value


class OpenAICompatProvider:
    """Bounded OpenAI Chat Completions transport with optional SSE streaming."""

    def __init__(
        self,
        config: OpenAICompatProviderConfig,
        *,
        secret_resolver: EnvironmentSecretResolver | None = None,
    ) -> None:
        self.config = config
        self.secret_resolver = secret_resolver or EnvironmentSecretResolver()
        self.endpoint = self._validate_and_build_endpoint(config)

    @staticmethod
    def _validate_and_build_endpoint(config: OpenAICompatProviderConfig) -> str:
        if not config.model.strip():
            raise ValueError("Provider model must be non-empty")
        if config.timeout_seconds <= 0 or config.max_response_bytes <= 0:
            raise ValueError("Provider timeout and response byte limit must be positive")
        if config.input_cost_per_million < 0 or config.output_cost_per_million < 0:
            raise ValueError("Provider token prices cannot be negative")
        if (
            not config.credential_header
            or not config.credential_header.isascii()
            or any(not (char.isalnum() or char == "-") for char in config.credential_header)
        ):
            raise ValueError("Provider credential header must be a safe HTTP header name")
        if "\r" in config.credential_prefix or "\n" in config.credential_prefix:
            raise ValueError("Provider credential prefix cannot contain a line break")
        parsed = urlsplit(config.base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Provider base URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Provider base URL cannot contain credentials, query, or fragment")
        if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
            raise ValueError("Remote provider endpoints require HTTPS")
        return config.base_url.rstrip("/") + "/chat/completions"

    async def complete(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        api_key = (
            self.secret_resolver.resolve(self.config.api_key_env)
            if self.config.api_key_env
            else None
        )
        return await self._await_network(
            self._complete_sync,
            request,
            api_key,
            cancellation=cancellation,
            task_name=f"openai-compat:{request.run_id}:{request.call_index}",
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
        api_key = (
            self.secret_resolver.resolve(self.config.api_key_env)
            if self.config.api_key_env
            else None
        )
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
            name=f"openai-compat-stream:{request.run_id}:{request.call_index}",
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
        api_key = (
            self.secret_resolver.resolve(self.config.api_key_env)
            if self.config.api_key_env
            else None
        )
        return await self._await_network(
            self._complete_structured_sync,
            request,
            api_key,
            cancellation=cancellation,
            task_name=f"openai-compat-structured:{request.request_id}:{request.call_index}",
        )

    async def _await_network(
        self,
        sync_call,
        request,
        api_key: str | None,
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
            # Cancelling the asyncio wrapper cannot stop a urllib worker thread,
            # but it must still release the task owned by this provider call.
            network_task.cancel()
            await asyncio.gather(network_task, return_exceptions=True)
            raise
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    def _complete_sync(self, request: ModelRequest, api_key: str | None) -> ModelResponse:
        payload = self._request_payload(request)
        return self._parse_response(self._post_payload(payload, api_key))

    def _complete_stream_sync(
        self,
        request: ModelRequest,
        api_key: str | None,
        progress: Callable[[ModelStreamProgress], None],
        stopped: threading.Event,
    ) -> ModelResponse:
        payload = self._request_payload(request)
        payload["stream"] = True
        if self.config.stream_include_usage:
            payload["stream_options"] = {"include_usage": True}
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

        raw_lines: list[bytes] = []
        chunks = 0
        received_chars = 0
        content_parts: list[str] = []
        tool_parts: dict[int, dict[str, str]] = {}
        usage: dict = {}
        request_id: str | None = None
        finish_reason = "unknown"
        saw_sse = False
        total_bytes = 0
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
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                saw_sse = True
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                    sanitize_wire_payload(event)
                except (json.JSONDecodeError, ValueError):
                    raise _invalid_response() from None
                if not isinstance(event, dict):
                    raise _invalid_response()
                if isinstance(event.get("error"), dict):
                    raise ModelProviderError(
                        "MODEL_UPSTREAM_ERROR",
                        "Model provider ended the stream with an error.",
                        retryable=True,
                    )
                if event.get("id"):
                    request_id = str(event["id"])
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                choices = event.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    raise _invalid_response()
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                delta_chars = 0
                content = delta.get("content")
                if isinstance(content, str):
                    content_parts.append(content)
                    delta_chars += len(content)
                raw_tool_calls = delta.get("tool_calls") or []
                if not isinstance(raw_tool_calls, list):
                    raise _invalid_response()
                for raw_call in raw_tool_calls:
                    if not isinstance(raw_call, dict):
                        raise _invalid_response()
                    index = _non_negative_int(raw_call.get("index"))
                    target = tool_parts.setdefault(
                        index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    if isinstance(raw_call.get("id"), str):
                        target["id"] += raw_call["id"]
                    function = raw_call.get("function")
                    if isinstance(function, dict):
                        if isinstance(function.get("name"), str):
                            target["name"] += function["name"]
                        if isinstance(function.get("arguments"), str):
                            target["arguments"] += function["arguments"]
                            delta_chars += len(function["arguments"])
                if delta_chars:
                    chunks += 1
                    received_chars += delta_chars
                    progress(ModelStreamProgress(chunks, received_chars))

        if not saw_sse:
            try:
                ordinary = json.loads(b"".join(raw_lines).decode("utf-8"))
                sanitize_wire_payload(ordinary)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                raise _invalid_response() from None
            result = self._parse_response(ordinary)
            progress(ModelStreamProgress(1, len(result.content), True))
            return result

        tool_calls = [
            {
                "id": value["id"],
                "type": "function",
                "function": {
                    "name": value["name"],
                    "arguments": value["arguments"],
                },
            }
            for _, value in sorted(tool_parts.items())
        ]
        assembled = {
            "id": request_id,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "".join(content_parts),
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        }
        result = self._parse_response(assembled)
        progress(ModelStreamProgress(chunks, received_chars, True))
        return result

    def _complete_structured_sync(
        self,
        request: StructuredOutputRequest,
        api_key: str | None,
    ) -> StructuredOutputResponse:
        payload = {
            "model": self.config.model,
            "messages": [_message_payload(message) for message in request.messages],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.json_schema,
                },
            },
            "stream": False,
        }
        return self._parse_structured_response(self._post_payload(payload, api_key))

    def _post_payload(self, payload: dict, api_key: str | None) -> dict:
        http_request = self._build_http_request(payload, api_key, accept="application/json")
        try:
            with urllib.request.urlopen(http_request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(self.config.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise _http_failure(exc.code) from None
        except (TimeoutError, socket.timeout):
            raise ModelProviderError(
                "MODEL_TIMEOUT",
                "Model provider request timed out.",
                retryable=True,
            ) from None
        except urllib.error.URLError:
            raise ModelProviderError(
                "MODEL_TRANSPORT_ERROR",
                "Model provider connection failed.",
                retryable=True,
            ) from None
        if len(raw) > self.config.max_response_bytes:
            raise ModelProviderError(
                "MODEL_RESPONSE_TOO_LARGE",
                "Model provider response exceeded the configured byte limit.",
                retryable=False,
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ModelProviderError(
                "MODEL_RESPONSE_INVALID",
                "Model provider returned invalid JSON.",
                retryable=True,
            ) from None
        try:
            sanitize_wire_payload(payload)
        except ValueError:
            raise ModelProviderError(
                "MODEL_RESPONSE_INVALID",
                "Model provider returned an unsafe JSON structure.",
                retryable=True,
            ) from None
        if not isinstance(payload, dict):
            raise _invalid_response()
        return payload

    def _build_http_request(
        self,
        payload: dict,
        api_key: str | None,
        *,
        accept: str,
    ) -> urllib.request.Request:
        try:
            sanitize_wire_payload(payload)
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, UnicodeEncodeError, ValueError):
            raise ModelProviderError(
                "MODEL_REQUEST_INVALID",
                "Model provider request could not be encoded safely.",
                retryable=False,
            ) from None
        headers = {
            "Accept": accept,
            "Content-Type": "application/json",
            "User-Agent": f"noruct/{__version__}",
            "Connection": "close",
        }
        if api_key:
            headers[self.config.credential_header] = f"{self.config.credential_prefix}{api_key}"
        return urllib.request.Request(
            self.endpoint,
            data=body,
            headers=headers,
            method="POST",
        )

    def _request_payload(self, request: ModelRequest) -> dict:
        return {
            "model": self.config.model,
            "messages": [_message_payload(message) for message in request.messages],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ],
            "response_format": _completion_response_format(),
            "stream": False,
        }

    def _parse_response(self, payload) -> ModelResponse:
        if not isinstance(payload, dict):
            raise _invalid_response()
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise _invalid_response()
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise _invalid_response()
        content = message.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise _invalid_response()
        raw_tool_calls = message.get("tool_calls") or []
        if not isinstance(raw_tool_calls, list):
            raise _invalid_response()
        tool_calls = tuple(_parse_tool_call(item) for item in raw_tool_calls)
        completion = None if tool_calls else _parse_completion(content)
        usage_payload = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        prompt_details = (
            usage_payload.get("prompt_tokens_details")
            if isinstance(usage_payload.get("prompt_tokens_details"), dict)
            else {}
        )
        input_tokens = _non_negative_int(usage_payload.get("prompt_tokens"))
        output_tokens = _non_negative_int(usage_payload.get("completion_tokens"))
        cached_tokens = _non_negative_int(prompt_details.get("cached_tokens"))
        cost = (
            input_tokens * self.config.input_cost_per_million
            + output_tokens * self.config.output_cost_per_million
        ) / 1_000_000
        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            completion=completion,
            usage=Usage(
                input_tokens=input_tokens,
                cached_input_tokens=cached_tokens,
                output_tokens=output_tokens,
                cost_usd=round(cost, 12),
            ),
            provider_request_id=str(payload.get("id")) if payload.get("id") else None,
            finish_reason=str(choice.get("finish_reason") or "unknown"),
        )

    def _parse_structured_response(self, payload) -> StructuredOutputResponse:
        if not isinstance(payload, dict):
            raise _invalid_response()
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise _invalid_response()
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise _invalid_response()
        try:
            value = json.loads(message["content"])
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
        usage_payload = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        prompt_details = (
            usage_payload.get("prompt_tokens_details")
            if isinstance(usage_payload.get("prompt_tokens_details"), dict)
            else {}
        )
        input_tokens = _non_negative_int(usage_payload.get("prompt_tokens"))
        output_tokens = _non_negative_int(usage_payload.get("completion_tokens"))
        cost = (
            input_tokens * self.config.input_cost_per_million
            + output_tokens * self.config.output_cost_per_million
        ) / 1_000_000
        return StructuredOutputResponse(
            value=value,
            usage=Usage(
                input_tokens=input_tokens,
                cached_input_tokens=_non_negative_int(prompt_details.get("cached_tokens")),
                output_tokens=output_tokens,
                cost_usd=round(cost, 12),
            ),
            provider_request_id=str(payload.get("id")) if payload.get("id") else None,
            finish_reason=str(choice.get("finish_reason") or "unknown"),
        )


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _http_failure(status: int) -> ModelProviderError:
    if status in {401, 403}:
        return ModelProviderError(
            "MODEL_AUTH_FAILED",
            "Model provider rejected the configured credential.",
            retryable=False,
        )
    if status == 429:
        return ModelProviderError(
            "MODEL_RATE_LIMITED",
            "Model provider rate limit was reached.",
            retryable=True,
        )
    if status >= 500:
        return ModelProviderError(
            "MODEL_UPSTREAM_ERROR",
            "Model provider is temporarily unavailable.",
            retryable=True,
        )
    return ModelProviderError(
        "MODEL_REQUEST_REJECTED",
        f"Model provider rejected the request with HTTP {status}.",
        retryable=False,
    )


def _invalid_response() -> ModelProviderError:
    return ModelProviderError(
        "MODEL_RESPONSE_INVALID",
        "Model provider response did not match the Chat Completions contract.",
        retryable=True,
    )


def _message_payload(message: ModelMessage) -> dict:
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": _content_text(message.content),
        }
    if message.role == "assistant" and isinstance(message.content, dict):
        calls = message.content.get("tool_calls") or []
        payload = {
            "role": "assistant",
            "content": message.content.get("content") or None,
        }
        if calls:
            payload["tool_calls"] = [
                {
                    "id": str(call.get("call_id", "")),
                    "type": "function",
                    "function": {
                        "name": str(call.get("name", "")),
                        "arguments": json.dumps(
                            call.get("arguments", {}),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                }
                for call in calls
                if isinstance(call, dict)
            ]
        return payload
    return {"role": message.role, "content": _content_text(message.content)}


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_tool_call(value) -> ToolCall:
    if not isinstance(value, dict):
        raise _invalid_response()
    function = value.get("function")
    if not isinstance(function, dict):
        raise _invalid_response()
    call_id = value.get("id")
    function_name = function.get("name")
    if not isinstance(call_id, str) or not call_id or not isinstance(function_name, str) or not function_name:
        raise _invalid_response()
    raw_arguments = function.get("arguments", "")
    if not isinstance(raw_arguments, str):
        arguments = {"_provider_arguments_error": "Arguments were not JSON text."}
    else:
        parsed = parse_tool_arguments(raw_arguments, function_name)
        arguments = (
            parsed
            if isinstance(parsed, dict)
            else {"_provider_arguments_error": "Arguments were not a repairable JSON object."}
        )
    return ToolCall(
        call_id=call_id,
        name=function_name,
        arguments=arguments,
    )


def _parse_completion(content: str) -> CompletionEnvelope | None:
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("summary"), str):
        return None
    tuple_fields = {}
    for key in (
        "artifact_refs",
        "acceptance_evidence",
        "unresolved_issues",
        "suggested_followups",
        "observations",
    ):
        items = value.get(key, [])
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            return None
        tuple_fields[key] = tuple(items)
    raw_signals = value.get("signals", [])
    if not isinstance(raw_signals, list):
        return None
    signals: list[RunSignal] = []
    try:
        for item in raw_signals:
            if not isinstance(item, dict) or not isinstance(item.get("value", ""), str):
                return None
            evidence = item.get("evidence", [])
            if not isinstance(evidence, list) or not all(isinstance(entry, str) for entry in evidence):
                return None
            semantic_replan = None
            if item.get("semantic_replan") is not None:
                try:
                    semantic_replan = semantic_replan_directive_from_dict(item["semantic_replan"])
                except (KeyError, TypeError, ValueError):
                    # Preserve the independently valid signal but never turn a
                    # malformed model payload into legacy topology syntax.
                    semantic_replan = None
            signals.append(
                RunSignal(
                    code=SignalCode(item["code"]),
                    value=item.get("value", ""),
                    evidence=tuple(evidence),
                    semantic_replan=semantic_replan,
                )
            )
    except (KeyError, ValueError):
        return None
    return CompletionEnvelope(
        summary=value["summary"],
        artifact_refs=tuple_fields["artifact_refs"],
        acceptance_evidence=tuple_fields["acceptance_evidence"],
        unresolved_issues=tuple_fields["unresolved_issues"],
        suggested_followups=tuple_fields["suggested_followups"],
        observations=tuple_fields["observations"],
        signals=tuple(signals),
    )


def _non_negative_int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _completion_response_format() -> dict:
    string_array = {"type": "array", "items": {"type": "string"}}
    semantic_replan = {
        "anyOf": [
            {"type": "null"},
            {
                "type": "object",
                "description": (
                    "Optional bounded replan intent. It is evidence only: the "
                    "Firm Kernel reconstructs and validates any actual graph patch."
                ),
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [operation.value for operation in SemanticReplanOperation],
                    },
                    "task_ids": string_array,
                    "capability_ids": string_array,
                    "assumption_refs": string_array,
                    "constraint_refs": string_array,
                },
                "required": [
                    "operation",
                    "task_ids",
                    "capability_ids",
                    "assumption_refs",
                    "constraint_refs",
                ],
                "additionalProperties": False,
            },
        ]
    }
    schema = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "The final deliverable text. Put every task-required exact line "
                    "or output field, including validation-repair requirements, in "
                    "summary rather than only in supporting arrays."
                ),
            },
            "artifact_refs": string_array,
            "acceptance_evidence": {
                **string_array,
                "description": (
                    "Supporting evidence only; this field does not replace required "
                    "content in summary."
                ),
            },
            "unresolved_issues": string_array,
            "suggested_followups": string_array,
            "observations": string_array,
            "signals": {
                "type": "array",
                "description": (
                    "Typed runtime signals only. A signal does not replace required "
                    "summary fields."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "enum": [code.value for code in SignalCode],
                        },
                        "value": {"type": "string"},
                        "evidence": string_array,
                        "semantic_replan": semantic_replan,
                    },
                    "required": ["code", "value", "evidence", "semantic_replan"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "summary",
            "artifact_refs",
            "acceptance_evidence",
            "unresolved_issues",
            "suggested_followups",
            "observations",
            "signals",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "dynamic_firm_employee_completion",
            "strict": True,
            "schema": schema,
        },
    }
